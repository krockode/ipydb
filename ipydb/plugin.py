# -*- coding: utf-8 -*-

"""
The ipydb plugin.

:copyright: (c) 2012 by Jay Sweeney.
:license: see LICENSE for more details.
"""
from ConfigParser import ConfigParser
from collections import defaultdict
import csv
import itertools
import fnmatch
import os
import re
import sys
import sqlalchemy as sa
from sqlalchemy.sql.compiler import RESERVED_WORDS
from IPython.core.plugin import Plugin
from termsize import termsize
from magic import SqlMagics
from metadata import CompletionDataAccessor
from ipydb import CONFIG_FILE, PLUGIN_NAME
import urlparse


def getconfigs():
    """Return a dictionary of saved database connection configurations."""
    cp = ConfigParser()
    cp.read(CONFIG_FILE)
    configs = {}
    default = None
    for section in cp.sections():
        conf = dict(cp.defaults())
        conf.update(dict(cp.items(section)))
        if conf.get('default'):
            default = section
        configs[section] = conf
    return default, configs


def sublists(l, n):
    return (l[i:i + n] for i in range(0, len(l), n))


def isublists(l, n):
    return itertools.izip_longest(*[iter(l)] * n)


def ipydb_completer(self, event):
    """Returns a list of suggested completions for text.

    Note: This is bound to an ipython shell instance
          and called on tab-presses by ipython.
    Args:
        event: see IPython.core.completer
    Returns:
        A list of candidate strings which complete the input text
        or None to propagate completion to other handlers or
        return [] to suppress further completion
    """
    try:
        sqlplugin = self.plugin_manager.get_plugin(PLUGIN_NAME)
        if sqlplugin:
            if sqlplugin.debug:
                print 'complete: sym=[%s] line=[%s] tuc=[%s]' % (event.symbol,
                    event.line, event.text_until_cursor)
            completions = sqlplugin.complete(event)
            if sqlplugin.debug:
                print 'completions:', completions
            return completions
    except Exception, e:
        print repr(e)
    return None


class FakedResult(object):

    def __init__(self, items, headings):
        self.items = items
        self.headings = headings

    def __iter__(self):
        return iter(self.items)

    def keys(self):
        return self.headings


class MonkeyString(str):
    """This is to avoid the restriction in
    i.c.completer.IPCompleter.dispatch_custom_completer where
    matches must begin with the text being matched."""

    def __new__(self, text, completion):
        self.text = text
        return str.__new__(self, completion)

    def startswith(self, text):
        if self.text == text:
            return True
        else:
            return super(MonkeyString, self).startswith(text)


class SqlPlugin(Plugin):
    """The ipydb plugin - manipulate databases from ipython."""

    max_fieldsize = 100  # configurable?
    completion_data = CompletionDataAccessor()
    sqlformats = "table csv".split()
    not_connected_message = "ipydb is not connected to a database. " \
        "Try:\n\t%connect CONFIGNAME\nor try:\n\t" \
        "%connect_url dbdriver://user:pass@host/dbname\n"
    completion_starters = "what_references show_fields connect " \
        "sql select insert update delete sqlformat".split()

    def __init__(self, shell=None, config=None):
        """Constructor.

        Args:
            shell: An instance of IPython.core.InteractiveShell.
            config: IPython's config object.
        """
        super(SqlPlugin, self).__init__(shell=shell, config=config)
        self.auto_magics = SqlMagics(self, shell)
        shell.register_magics(self.auto_magics)
        self.sqlformat = 'table'  # 'table' | 'csv'
        self.do_reflection = True
        self.connected = False
        self.engine = None
        self.nickname = None
        self.autocommit = True
        self.trans_ctx = None
        self.debug = False
        default, configs = getconfigs()
        self.init_completer()
        if default:
            self.connect(default)

    def init_completer(self):
        # self.shell.set_custom_completer(ipydb_completer)
        # to complete things like table.* we needto 
        # change the ipydb spliiter delims:
        delims = self.shell.Completer.splitter.delims.replace('*', '')
        self.shell.Completer.splitter.delim = delims
        if self.shell.Completer.readline:
            self.shell.Completer.readline.set_completer_delims(delims)
        for token in self.completion_starters:
            self.shell.set_hook('complete_command',
                                ipydb_completer, str_key=token)

    def get_engine(self):
        """Returns current sqlalchemy engine reference, if there was one."""
        if not self.connected:
            print self.not_connected_message
        return self.engine

    def get_db_ps1(self, *args, **kwargs):
        """ Return current host/db for use in ipython's prompt PS1. """
        if not self.connected:
            return ''
        host = self.engine.url.host
        if '.' in host:
            host = host.split('.')[0]
        host = host[:15]  # don't like long hostnames
        db = self.engine.url.database[:15]
        url = "%s/%s" % (host, db)
        if self.nickname:
            url = self.nickname
        return " " + url

    def get_transaction_ps1(self, *args, **kw):
        """Return '*' if ipydb has an active transaction."""
        if not self.connected:
            return ''
        # I want this: ⚡
        # but looks like IPython is expecting ascii for the PS1!?
        if self.trans_ctx and self.trans_ctx.transaction.is_active:
            return ' *'
        else:
            return ''

    def get_reflecting_ps1(self, *args, **kw):
        """
        Return a string indictor if background schema reflection is running.
        """
        if not self.connected:
            return ''
        return ' !' if self.completion_data.reflecting(self.engine) else ''

    def safe_url(self, url_string):
        """Return url_string with password removed."""
        url = None
        try:
            url = sa.engine.url.make_url(str(url_string))
            url.password = 'xxx'
        except:
            pass
        return url

    def connect(self, configname=None):
        """Connect to a database based upon its `nickname`.

        See ipydb.magic.connect() for details.
        """
        default, configs = getconfigs()

        def available():
            print self.connect.__doc__
            print "Available config names: %s" % (
                ' '.join(sorted(configs.keys())))
        if not configname:
            available()
        elif configname not in configs:
            print "Config `%s` not found. " % configname
            available()
        else:
            config = configs[configname]
            connect_args = {}
            self.connect_url(self.make_connection_url(config),
                             connect_args=connect_args)
            self.nickname = configname
        return self.connected

    @property
    def metadata(self):
        """Get sqlalchemy.MetaData instance for current connection."""
        if not self.connected:
            return None
        meta = getattr(self, '_metadata', None)
        if meta is None or self._metadata.bind != self.engine:
            self._metadata = sa.MetaData(bind=self.engine)
        return self._metadata

    def connect_url(self, url, connect_args={}):
        """Connect to a database using an SqlAlchemy URL.

        Args:
            url: An SqlAlchemy-style DB connection URL.
            connect_args: extra argument to be passed to the underlying
                          DB-API driver.
        Returns:
            True if connection was successful.
        """
        if self.trans_ctx and self.trans_ctx.transaction.is_active:
            print "You have an active transaction, either %commit or " \
                "%rollback before connecting to a new database."
            return
        safe_url = self.safe_url(url)
        if safe_url:
            print "ipydb is connecting to: %s" % safe_url
        if safe_url.drivername == 'oracle':
            # not sure why we need this horrible hack -
            # I think there's some weirdness
            # with cx_oracle/oracle versions I'm using.
            os.environ["NLS_LANG"] = ".AL32UTF8"
            import cx_Oracle
            if not getattr(cx_Oracle, '_cxmakedsn', None):
                setattr(cx_Oracle, '_cxmakedsn', cx_Oracle.makedsn)

                def newmakedsn(*args, **kw):
                    return cx_Oracle._cxmakedsn(*args, **kw).replace(
                        'SID', 'SERVICE_NAME')
                cx_Oracle.makedsn = newmakedsn
        elif safe_url.drivername == 'mysql':
            import MySQLdb.cursors
            # use server-side cursors by default (does this work with myISAM?)
            connect_args = {'cursorclass': MySQLdb.cursors.SSCursor}
        self.engine = sa.engine.create_engine(url, connect_args=connect_args)
        self.connected = True
        self.nickname = None
        if self.do_reflection:
            self.completion_data.get_metadata(self.engine)
        return True

    def flush_metadata(self):
        """Delete cached schema information"""
        print "Deleting metadata..."
        self.completion_data.flush()
        if self.connected:
            self.completion_data.get_metadata(self.engine)

    def make_connection_url(self, config):
        """
        Returns an SqlAlchemy connection URL based upon values in config dict.

        Args:
            config: dict-like object with keys: type, username, password,
                    host, and database.
        Returns:
            str URL which SqlAlchemy can use to connect to a database.
        """
        cfg = defaultdict(str)
        cfg.update(config)
        return sa.engine.url.URL(
            drivername=cfg['type'], username=cfg['username'],
            password=cfg['password'], host=cfg['host'],
            database=cfg['database'],
            query=dict(urlparse.parse_qsl(cfg['query'])))

    def execute(self, query):
        """Execute query against current db connection, return result set.

        Args:
            query: string query to execute
        Returns:
            Sqlalchemy's DB-API cursor-like object.
        """
        result = None
        if not self.connected:
            print self.not_connected_message
        else:
            bits = query.split()
            if len(bits) == 2 and bits[0].lower() == 'select' and \
                    bits[1] in self.completion_data.tables(self.engine):
                query = 'select * from %s' % bits[1]
            conn = self.engine
            if self.trans_ctx and self.trans_ctx.transaction.is_active:
                conn = self.trans_ctx.conn.execute
            try:
                result = conn.execute(query)
            except Exception, e:
                if self.debug:
                    raise
                print e.message
        return result

    def begin(self):
        """Start a new transaction against the current db connection."""
        if not self.connected:
            print self.not_connected_message
            return
        if not self.trans_ctx or not self.trans_ctx.transaction.is_active:
            self.trans_ctx = self.engine.begin()
        else:
            print "You are already in a transaction" \
                " block and nesting is not supported"

    def commit(self):
        """Commit current transaction if there was one."""
        if not self.connected:
            print self.not_connected_message
            return
        if self.trans_ctx:
            with self.trans_ctx:
                pass
            self.trans_ctx = None
        else:
            print "No active transaction"

    def rollback(self):
        """Rollback current transaction if there was one."""
        if not self.connected:
            print self.not_connected_message
            return
        if self.trans_ctx:
            self.trans_ctx.transaction.rollback()
            self.trans_ctx = None
        else:
            print "No active transaction"

    def show_tables(self, *globs):
        """Print a list of tablenames matching input glob/s.

        All table names are printed if no glob is given, otherwise
        just those table names matching any of the *globs are printed.

        Args:
            *glob: zero or more globs to match against table names.

        """
        if not self.connected:
            print self.not_connected_message
            return
        matches = set()
        tablenames = self.completion_data.tables(self.engine)
        if not globs:
            matches = tablenames
        else:
            for glob in globs:
                matches.update(fnmatch.filter(tablenames, glob))
        self.render_result(FakedResult(((r,) for r in matches), ['Table']))
        # print '\n'.join(sorted(matches))

    def show_fields(self, *globs):
        """
        Print a list of fields matching the input glob tableglob[.fieldglob].

        See ipydb.magic.show_fields for examples.

        Args:
            *globs: list of [tableglob].[fieldglob] strings
        """
        if not self.connected:
            print self.not_connected_message
            return
        matches = set()
        dottedfields = self.completion_data.dottedfields(self.engine)
        if not globs:
            matches = dottedfields
        for glob in globs:
            bits = glob.split('.', 1)
            if len(bits) == 1:  # table name only
                glob += '.*'
            matches.update(fnmatch.filter(dottedfields, glob))
        tprev = None
        try:
            out = self.get_pager()
            for match in sorted(matches):
                tablename, fieldname = match.split('.', 1)
                if tablename != tprev:
                    if tprev is not None:
                        out.write("\n")
                    out.write(tablename + '\n')
                    out.write('-' * len(tablename) + '\n')
                out.write("    %-35s%s\n" % (
                    fieldname,
                    self.completion_data.types(self.engine).get(match, '[?]')))
                tprev = tablename
            out.write('\n')
        except IOError, msg:
            if msg.args == (32, 'Broken pipe'):  # user quit
                pass
            else:
                raise
        finally:
            out.close()

    def what_references(self, arg):
        """Show fields referencing the input table/field arg.

        If arg is a tablename, then print fields which reference
        any field in tablename. If arg is a field (specified by
        tablename.fieldname), then print only fields which reference
        the specified table.field.

        Args:
            arg: Either a table name or a [table.field] name"""
        if not self.connected:
            print self.not_connected_message
            return
        bits = arg.split('.', 1)
        tablename = bits[0]
        fieldname = bits[1] if len(bits) > 1 else ''
        field = table = None
        meta = self.completion_data.sa_metadata
        meta.reflect()  # XXX: can be very slow! TODO: don't do this
        for tname, tbl in meta.tables.iteritems():
            if tbl.name.lower() == tablename.lower():
                table = tbl
                break
        if table is None:
            print "Could not find table `%s`" % (tablename,)
            return
        if fieldname:
            for col in table.columns:
                if col.name == fieldname:
                    field = col
                    break
        if fieldname and field is None:
            print "Could not find `%s.%s`" % (tablename, fieldname)
            return
        refs = []
        for tname, tbl in meta.tables.iteritems():
            for fk in tbl.foreign_keys:
                if ((field is not None and fk.references(table) and
                        bool(fk.get_referent(table) == field)) or
                        (field is None and fk.references(table))):
                    sourcefield = "%s.%s" % (
                        fk.parent.table.name, fk.parent.name)
                    refs.append((sourcefield, fk.target_fullname))
        if refs:
            maxleft = max(map(lambda x: len(x[0]), refs)) + 2
            fmt = u"%%-%ss references %%s" % (maxleft,)
        for ref in sorted(refs, key=lambda x: x[0]):
            print fmt % ref

    def get_pager(self):
        return os.popen('less -FXRiS', 'w')  # XXX: use ipython's pager

    def render_result(self, cursor):
        """Render a result set and pipe through less.

        Args:
            cursor: iterable of tuples, with one special method:
                    cursor.keys() which returns a list of string columns
                    headings for the tuples.
        """
        try:
            out = self.get_pager()
            if self.sqlformat == 'csv':
                self.format_result_csv(cursor, out=out)
            else:
                self.format_result_pretty(cursor, out=out)
        except IOError, msg:
            if msg.args == (32, 'Broken pipe'):  # user quit
                pass
            else:
                raise
        finally:
            out.close()

    def format_result_pretty(self, cursor, out=sys.stdout):
        """Render an SQL result set as an ascii-table.

        Renders an SQL result set to `out`, some file-like object.
        Assumes that we can determine the current terminal height and
        width via the termsize module.

        Args:
            cursor: cursor-like object. See: render_result()
            out: file-like object.

        """
        cols, lines = termsize()
        headings = cursor.keys()
        heading_sizes = map(lambda x: len(x), headings)
        for screenrows in isublists(cursor, lines - 4):
            sizes = heading_sizes[:]
            for row in screenrows:
                if row is None:
                    break
                for idx, value in enumerate(row):
                    if not isinstance(value, basestring):
                        value = str(value)
                    size = max(sizes[idx], len(value))
                    sizes[idx] = min(size, self.max_fieldsize)
            for size in sizes:
                out.write('+' + '-' * (size + 2))
            out.write('+\n')
            for idx, size in enumerate(sizes):
                fmt = '| %%-%is ' % size
                out.write((fmt % headings[idx]))
            out.write('|\n')
            for size in sizes:
                out.write('+' + '-' * (size + 2))
            out.write('+\n')
            for rw in screenrows:
                if rw is None:
                    break  # from isublists impl
                for idx, size in enumerate(sizes):
                    fmt = '| %%-%is ' % size
                    value = rw[idx]
                    if not isinstance(value, basestring):
                        value = str(value)
                    if len(value) > self.max_fieldsize:
                        value = value[:self.max_fieldsize - 5] + '[...]'
                    value = value.replace('\n', '^')
                    value = value.replace('\r', '^').replace('\t', ' ')
                    out.write((fmt % value))
                out.write('|\n')

    def format_result_csv(self, cursor, out=sys.stdout):
        """Render an sql result set in CSV format.

        Args:
            result: cursor-like object: see render_result()
            out: file-like object to write results to.
        """
        writer = csv.writer(out)
        writer.writerow(cursor.keys())
        writer.writerows(cursor)

    def interested_in(self, event):
        """Return True if ipydb is interested in completions for line_buffer.

        Args:
            text: Current token (str) of text being completed.
            line_buffer: str text for the whole line.
        Returns:
            True if ipydb should try to complete text, False otherwise.
        """
        line_buffer, text = event.line, event.symbol
        if text and not line_buffer:
            return True  # this is unfortunate...
        else:
            first_token = line_buffer.split()[0].lstrip('%')
            if first_token in self.completion_starters:
                return True
            magic_assignment_re = r'^\s*\S+\s*=\s*%({magics})'.format(
                magics='|'.join(self.completion_starters))
            return re.match(magic_assignment_re, line_buffer) is not None

    def complete(self, event):
        """Return a list of "tab-completion" strings for text.

        Args:
            event: see IPython.core.completer
        Returns:
            list of strings which can complete the input text.
        """
        text, line_buffer = event.symbol, event.line
        matches = []
        matches_append = matches.append
        if not self.interested_in(event):
            return None 
        first_token = None
        if line_buffer:
            first_token = line_buffer.split()[0].lstrip('%')
        if first_token == 'connect':
            keys = getconfigs()[1].keys()
            self.match_lists([keys], text, matches_append)
            return matches or None
        if first_token == 'sqlformat':
            self.match_lists([self.sqlformats], text, matches_append)
            return matches or None
        event.first_token = first_token
        return self.complete_sql(event)

    def match_lists(self, lists, text, appendfunc):
        """Helper to substring-match text in a list-of-lists.

        Args:
            lists: a list of lists of strings.
            text: text to substring match against lists.
            appendfunc: callable, called with each string from
                        and of the input lists that can complete
                        text - appendfunc(match)
        """
        n = len(text)
        for word in itertools.chain(*lists):
            if word[:n] == text:
                appendfunc(word)

    def complete_sql(self, event):
        """Return completion suggestions based up database schema terms.

        See complete() for keyword arguments.

        Args:
            first_token: The first non-whitespace token from the front
                         of line_buffer.
        Returns:
            A List of strings which can complete input text.
        """
        text, line_buffer, first_token = (event.symbol, event.line,
                                          event.first_token)
        text_until_cursor = event.text_until_cursor
        if not self.connected:
            return None 
        matches = []
        matches_append = matches.append
        metadata = self.completion_data.get_metadata(self.engine, noisy=False)
        dottedfields = metadata['dottedfields']
        fields = metadata['fields']
        tables = metadata['tables']
        if line_buffer and len(line_buffer.split()) == 2:
            # check for select table_name<tab>
            first, second = line_buffer.split()
            if first in ('select', 'insert') and second in tables:
                cols = []
                dcols = []
                for f in dottedfields:
                    tablename = f.split('.')[0]
                    if second == tablename:
                        dcols.append(f)
                        cols.append(f.split('.')[1])
                colstr = ', '.join(sorted(cols))
                if first == 'select':
                    return [MonkeyString(event.symbol, 
                            '%s from %s order by %s' %
                            (colstr, second, cols[0]))]
                else:
                    deflt = []
                    types = self.completion_data.types(self.engine)
                    restr = re.compile(r'TEXT|VARCHAR.*|CHAR.*')
                    renumeric = re.compile(r'FLOAT.*|DECIMAL.*|INT.*'
                                           '|DOUBLE.*|FIXED.*|SHORT.*')
                    redate = re.compile(r'DATE|TIME|DATETIME|TIMESTAMP')
                    for dc in sorted(dcols):
                        typ = types[dc]
                        tmpl = ''
                        if redate.search(typ):
                            tmpl = '""'  # XXX: now() or something?
                        elif restr.search(typ):
                            tmpl = '""'
                        elif renumeric.search(typ):
                            tmpl = '0'
                        deflt.append(tmpl)
                    return [MonkeyString(event.symbol,
                            'into %s (%s) values (%s)' %
                            (second, colstr, ', '.join(deflt)))]
        if event.symbol.count('.') == 1:
            head, tail = text.split('.')
            if head in tables and tail == '*':
                # tablename.*<tab> -> expand names
                dotted = []
                for f in dottedfields:
                    tab, fld = f.split('.')
                    if tab == head:
                        dotted.append(f)
                return [MonkeyString(event.symbol,
                        ', '.join(sorted(dotted)))]
            self.match_lists([dottedfields], text, matches_append)
            if not len(matches):
                # try for any field (following), could be
                # table alias that is not yet defined
                # (e.g. user typed `select foo.id, foo.<tab>...`)
                self.match_lists(
                    [tables], tail, lambda match: matches_append(head + '.' + match))
                if tail == '':
                    fields = map(lambda word: head + '.' + word, fields)
                    matches.extend(fields)
                return matches or None
        self.match_lists([tables, fields, RESERVED_WORDS],
                         text, matches_append)
        return matches or None
