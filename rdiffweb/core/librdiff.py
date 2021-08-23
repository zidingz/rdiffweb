# -*- coding: utf-8 -*-
# rdiffweb, A web interface to rdiff-backup repositories
# Copyright (C) 2019 rdiffweb contributors
#
# This program is free software: you can redistribute it and/or modify
# it under the terms of the GNU General Public License as published by
# the Free Software Foundation, either version 3 of the License, or
# (at your option) any later version.
#
# This program is distributed in the hope that it will be useful,
# but WITHOUT ANY WARRANTY; without even the implied warranty of
# MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the
# GNU General Public License for more details.
#
# You should have received a copy of the GNU General Public License
# along with this program.  If not, see <http://www.gnu.org/licenses/>.

import bisect
import calendar
from datetime import timedelta
import encodings
import logging
import os
import re
import shutil
import subprocess
import sys
import threading
import time
import weakref

import psutil
from rdiffweb.core import rdw_helpers
from rdiffweb.core.i18n import ugettext as _
from distutils import spawn
from subprocess import CalledProcessError

# Define the logger
logger = logging.getLogger(__name__)

# Constant for the rdiff-backup-data folder name.
RDIFF_BACKUP_DATA = b"rdiff-backup-data"

# Increment folder name.
INCREMENTS = b"increments"

# Define the default LANG environment variable to be passed to rdiff-backup
# restore command line to make sure the binary output stdout as utf8 otherwise
# we end up with \x encoded characters.
STDOUT_ENCODING = 'utf-8'
LANG = "en_US." + STDOUT_ENCODING

# PATH for executable lookup
PATH = path = os.path.dirname(sys.executable) + os.pathsep + os.environ['PATH']

PID_RE = re.compile(b"^PID\s*([0-9]+)", re.I | re.M)


def rdiff_backup_version():
    """
    Get rdiff-backup version
    """
    try:
        output = subprocess.check_output([find_rdiff_backup(), '--version'])
        m = re.search(b'([0-9]+).([0-9]+).([0-9]+)', output)
        return (int(m.group(1)), int(m.group(2)), int(m.group(3)))
    except:
        return (0, 0, 0)


def find_rdiff_backup():
    """
    Lookup for `rdiff-backup` executable. Raise an exception if not found.
    """
    cmd = spawn.find_executable('rdiff-backup', PATH)
    if not cmd:
        raise ExecutableNotFoundError("can't find `rdiff-backup` executable in PATH: %s" % PATH)
    return os.fsencode(cmd)


def find_rdiff_backup_delete():
    """
    Lookup for `rdiff-backup-delete` executable. Raise an exception if not found.
    """
    cmd = spawn.find_executable('rdiff-backup-delete', PATH)
    if not cmd:
        raise ExecutableNotFoundError("can't find `rdiff-backup-delete` executable in PATH: %s, make sure you have rdiff-backup >= 2.0.1 installed" % PATH)
    return os.fsencode(cmd)

def popen(cmd, buffering=-1, stderr=None, env=None):
    """
    Alternative to os.popen() to support a `cmd` with a list of arguments and
    return a file object that return bytes instead of string.

    `stderr` could be subprocess.STDOUT or subprocess.DEVNULL or a function.
    Otherwise, the error is redirect to logger.
    """
    if buffering == 0 or buffering is None:
        raise ValueError("popen() does not support unbuffered streams")
    # Check if stderr should be pipe.
    pipe_stderr = stderr == subprocess.PIPE or hasattr(stderr, '__call__') or stderr is None
    proc = subprocess.Popen(cmd,
                            shell=False,
                            stdout=subprocess.PIPE,
                            stderr=subprocess.PIPE if pipe_stderr else stderr,
                            bufsize=buffering,
                            env=env)
    if pipe_stderr:
        t = threading.Thread(target=_readerthread, args=(proc.stderr, stderr))
        t.daemon = True
        t.start()
    return _wrap_close(proc.stdout, proc)

# Helper for popen() to redirect stderr to a logger.
def _readerthread(stream, func):
    """
    Read stderr and pipe each line to logger.
    """
    func = func or logger.debug
    for line in stream:
        func(line.decode(STDOUT_ENCODING, 'replace').strip('\n'))
    stream.close()

# Helper for popen() to close process when the pipe is closed.
class _wrap_close:
    def __init__(self, stream, proc):
        self._stream = stream
        self._proc = proc
    def close(self):
        self._stream.close()
        returncode = self._proc.wait()
        if returncode == 0:
            return None
        return returncode
    def __enter__(self):
        return self
    def __exit__(self, *args):
        self.close()
    def __getattr__(self, name):
        return getattr(self._stream, name)
    def __iter__(self):
        return iter(self._stream)

class ExecutableNotFoundError(Exception):
    """
    Raised when rdiff-backup or rdiff-backup-delete can't be found. 
    """
    pass


class FileError(Exception):
    pass


class AccessDeniedError(FileError):
    pass


class DoesNotExistError(FileError):
    pass


class SymLinkAccessDeniedError(FileError):
    pass


class UnknownError(FileError):
    pass


class cached_property(object):
    """
    A property that is only computed once per instance and then replaces itself
    with an ordinary attribute.
    """

    def __init__(self, func):
        self.__doc__ = getattr(func, "__doc__")
        self.func = func

    def __get__(self, obj, cls):
        if obj is None:
            return self
        value = obj.__dict__[self.func.__name__] = self.func(obj)
        return value


class RdiffTime(object):

    """Time information has two components: the local time, stored in GMT as
    seconds since Epoch, and the timezone, stored as a seconds offset. Since
    the server may not be in the same timezone as the user, we cannot rely on
    the built-in localtime() functions, but look at the rdiff-backup string
    for timezone information.  As a general rule, we always display the
    "local" time, but pass the timezone information on to rdiff-backup, so
    it can restore to the correct state"""

    def __init__(self, value=None, tz_offset=None):
        assert value is None or isinstance(
            value, int) or isinstance(value, str)
        if value is None:
            # Get GMT time.
            self._time_seconds = int(time.time())
            self._tz_offset = 0
        elif isinstance(value, int):
            self._time_seconds = value
            self._tz_offset = tz_offset or 0
        elif isinstance(RdiffTime, int):
            self._time_seconds = value._time_seconds
            self._tz_offset = value._tz_offset
        else:
            self._from_str(value)

    def _from_str(self, timeString):
        try:
            date, daytime = timeString[:19].split("T")
            year, month, day = list(map(int, date.split("-")))
            hour, minute, second = list(map(int, daytime.split(":")))
            assert 1900 < year < 2100, year
            assert 1 <= month <= 12
            assert 1 <= day <= 31
            assert 0 <= hour <= 23
            assert 0 <= minute <= 59
            assert 0 <= second <= 61  # leap seconds

            timetuple = (year, month, day, hour, minute, second, -1, -1, 0)
            self._time_seconds = calendar.timegm(timetuple)
            self._tz_offset = self._tzdtoseconds(timeString[19:])
            self._tz_str()  # to get assertions there

        except (TypeError, ValueError, AssertionError):
            raise ValueError(timeString)

    def get_local_day_since_epoch(self):
        return self._time_seconds // (24 * 60 * 60)

    def epoch(self):
        return self._time_seconds - self._tz_offset

    def _tz_str(self):
        if self._tz_offset:
            hours, minutes = divmod(abs(self._tz_offset) // 60, 60)
            assert 0 <= hours <= 23
            assert 0 <= minutes <= 59
            if self._tz_offset > 0:
                plusMinus = "+"
            else:
                plusMinus = "-"
            return "%s%s:%s" % (plusMinus, "%02d" % hours, "%02d" % minutes)
        else:
            return "Z"

    def set_time(self, hour, minute, second):
        year = time.gmtime(self._time_seconds)[0]
        month = time.gmtime(self._time_seconds)[1]
        day = time.gmtime(self._time_seconds)[2]
        _time_seconds = calendar.timegm(
            (year, month, day, hour, minute, second, -1, -1, 0))
        return RdiffTime(_time_seconds, self._tz_offset)

    def _tzdtoseconds(self, tzd):
        """Given w3 compliant TZD, converts it to number of seconds from UTC"""
        if tzd == "Z":
            return 0
        assert len(tzd) == 6  # only accept forms like +08:00 for now
        assert (tzd[0] == "-" or tzd[0] == "+") and tzd[3] == ":"

        if tzd[0] == "+":
            plusMinus = 1
        else:
            plusMinus = -1

        return plusMinus * 60 * (60 * int(tzd[1:3]) + int(tzd[4:]))

    def __add__(self, other):
        """Support plus (+) timedelta"""
        assert isinstance(other, timedelta)
        return RdiffTime(self._time_seconds + int(other.total_seconds()), self._tz_offset)

    def __sub__(self, other):
        """Support minus (-) timedelta"""
        assert isinstance(other, timedelta) or isinstance(other, RdiffTime)
        # Sub with timedelta, return RdiffTime
        if isinstance(other, timedelta):
            return RdiffTime(self._time_seconds - int(other.total_seconds()), self._tz_offset)

        # Sub with RdiffTime, return timedelta
        if isinstance(other, RdiffTime):
            return timedelta(seconds=self._time_seconds - other._time_seconds)

    def __int__(self):
        """Return this date as seconds since epoch."""
        return self.epoch()

    def __lt__(self, other):
        assert isinstance(other, RdiffTime)
        return self.epoch() < other.epoch()

    def __le__(self, other):
        assert isinstance(other, RdiffTime)
        return self.epoch() <= other.epoch()

    def __gt__(self, other):
        assert isinstance(other, RdiffTime)
        return self.epoch() > other.epoch()

    def __ge__(self, other):
        assert isinstance(other, RdiffTime)
        return self.epoch() >= other.epoch()

    def __cmp__(self, other):
        assert isinstance(other, RdiffTime)
        return (self.epoch() > other.epoch()) - (self.epoch() < other.epoch())

    def __eq__(self, other):
        return (isinstance(other, RdiffTime) and
                self.epoch() == other.epoch())

    def __hash__(self):
        return hash(self.epoch())

    def __str__(self):
        """return utf-8 string"""
        value = time.strftime("%Y-%m-%dT%H:%M:%S",
                              time.gmtime(self._time_seconds))
        if isinstance(value, bytes):
            value = value.decode(encoding='latin1')
        return value + self._tz_str()

    def __repr__(self):
        """return second since epoch"""
        return "RdiffTime('" + str(self) + "')"

    __json__ = __str__


class DirEntry(object):

    """Includes name, isDir, fileSize, exists, and dict (changeDates) of sorted
    local dates when backed up"""

    def __init__(self, parent, path, exists, increments):
        assert isinstance(parent, RdiffRepo) or isinstance(parent, DirEntry)
        assert isinstance(path, bytes)

        # Keep reference to the path and repo object.
        if isinstance(parent, RdiffRepo):
            self._repo = parent
            self.path = path
        else:
            self._repo = parent._repo
            # Relative path to the repository.
            self.path = os.path.join(parent.path, path)
        # Absolute path to the directory
        self.full_path = os.path.normpath(
            os.path.join(self._repo.full_path, self.path))
        # May need to compute our own state if not provided.
        self.exists = exists
        # Store the increments sorted by date.
        # See self.last_change_date()
        self._increments = sorted(increments, key=lambda x: x.date)

    @property
    def dir_entries(self):
        """Get directory entries for the current path. It is similar to
        listdir() but for rdiff-backup."""

        logger.debug("get directory entries for [%r]", self.full_path)

        # Group increments by filename
        grouped_increment_entries = rdw_helpers.groupby(
            self._repo._get_increment_entries(self.path), lambda x: x.filename)

        # Check if the directory exists. It may not exist if
        # it has been delete
        existing_entries = []
        if os.path.isdir(self.full_path) and os.access(self.full_path, os.F_OK):
            # Get entries from directory structure
            existing_entries = os.listdir(self.full_path)
            # Remove "rdiff-backup-data" directory
            if self.isroot() and RDIFF_BACKUP_DATA in existing_entries:
                existing_entries.remove(RDIFF_BACKUP_DATA)

        # Process each increment entries and combine this with the existing
        # entries
        entriesDict = {}
        for filename, increments in grouped_increment_entries.items():
            # Check if filename exists
            exists = filename in existing_entries
            # Create DirEntry to represent the item
            new_entry = DirEntry(
                self,
                filename,
                exists,
                increments)
            entriesDict[filename] = new_entry

        # Then add existing entries
        for filename in existing_entries:
            # Check if the entry was created by increments entry
            if filename in entriesDict:
                continue
            # The entry doesn't exists (mostly because it ever change). So
            # create a DirEntry to represent it
            new_entry = DirEntry(
                self,
                filename,
                True,
                [])
            entriesDict[filename] = new_entry

        # Return the values (so the DirEntry objects)
        return list(entriesDict.values())

    @property
    def display_name(self):
        """Return the most human readable filename. Without quote."""
        if self.isroot():
            return self._repo.display_name
        value = self._repo.unquote(os.path.basename(self.path))
        return self._repo._decode(value)

    def isroot(self):
        """
        Check if the directory entry represent the root of the repository.
        Return True when path is empty.
        """
        return self.path == b''

    @property
    def isdir(self):
        """Lazy check if entry is a directory"""
        if hasattr(self, '_isdir'):
            return self._isdir
        if self.exists:
            # If the entry exists, check if it's a directory
            self._isdir = os.path.isdir(self.full_path)
        else:
            # Check if increments is a directory
            self._isdir = False
            for increment in self._increments:
                if increment.is_missing:
                    # Ignore missing increment...
                    continue
                self._isdir = increment.isdir
                break
        return self._isdir

    @property
    def file_size(self):
        """Return the file size in bytes."""
        if hasattr(self, '_file_size'):
            return self._file_size
        if self.exists:
            self._file_size = os.lstat(self.full_path).st_size
        else:
            # The only viable place to get the filesize of a deleted entry
            # it to get it from file_statistics
            try:
                stats = self._repo.file_statistics[self.last_change_date]
                # File stats uses unquoted name.
                unquote_path = self._repo.unquote(self.path)
                self._file_size = stats.get_source_size(unquote_path)
            except KeyError:
                logger.warning(
                    "cannot find file statistic [%s]", self.last_change_date)
                self._file_size = 0
        return self._file_size

    @property
    def change_dates(self):
        """
        Return a list of dates when this item has changes. Represent the
        previous revision. From old to new.
        """
        # Exception for root path, use backups dates.
        if self.isroot():
            return self._repo.backup_dates

        # Return previous computed value
        if hasattr(self, '_change_dates'):
            return self._change_dates

        # Compute the dates
        change_dates = set()
        for increment in self._increments:
            # Skip "invisible" increments
            if not increment.has_suffix:
                continue
            # Get date of the increment as reference
            change_date = increment.date
            # If the increment is a "missing" increment, need to get the date
            # before the folder was removed.
            if not increment.is_snapshot and increment.is_missing:
                change_date = self._get_first_backup_after_date(change_date)

            if change_date:
                change_dates.add(change_date)

        # If the directory exists, add the last known backup date.
        if self.exists and self._repo.last_backup_date:
            change_dates.add(self._repo.last_backup_date)

        # No need to sort the change date since increments are already sorted.

        # Return the list of dates.
        self._change_dates = sorted(change_dates)
        return self._change_dates

    def delete(self):
        """
        Delete this entry from the repository history using rdiff-backup-delete.
        """
        if self.isroot():
            self._repo.delete()
        else:
            rdiff_backup_delete = find_rdiff_backup_delete()
            cmdline = [rdiff_backup_delete, self.full_path]
            logger.info('executing: %r' % cmdline)
            process = subprocess.Popen(
                cmdline, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, env={'LANG': LANG})
            for line in process.stdout:
                line = line.rstrip(b'\n').decode('utf-8', errors='replace')
                logger.info('rdiff-backup-delete: %s' % line)
            retcode = process.wait()
            if retcode:
                raise CalledProcessError(retcode, cmdline)

    @property
    def first_change_date(self):
        """Return first change date or False."""
        return self.change_dates and self.change_dates[0]

    def _get_first_backup_after_date(self, date):
        """ Iterates the mirror_metadata files in the rdiff data dir """
        index = bisect.bisect_right(self._repo.backup_dates, date)
        # Check if index is in range.
        if index >= len(self._repo.backup_dates):
            return None
        return self._repo.backup_dates[index]

    @property
    def last_change_date(self):
        """Return last change date or False."""
        return self.change_dates and self.change_dates[-1]

    def restore(self, restore_as_of, kind):
        """
        Restore the current directory entry into a fileobj containing the
        file content of the directory compressed into an archive.

        Return a filename and a fileobj.
        """
        assert restore_as_of, "restore_as_of must be defined"

        # Define a nice filename for the archive or file to be created.
        # TODO The current entry might be a directory, but it may have been a
        # file.
        if self.path == b"" or self.isdir:
            kind = kind or 'zip'
            filename = "%s.%s" % (self.display_name, kind)
        else:
            kind = 'raw'
            filename = self.display_name

        # Restore data using a subprocess.
        from rdiffweb.core.restore import call_restore
        path = os.path.join(self._repo.full_path,
                            self._repo.unquote(self.path))
        fh = call_restore(path, restore_as_of, self._repo._encoding.name, kind)
        return filename, fh


class HistoryEntry(object):

    def __init__(self, repo, date):
        assert isinstance(repo, RdiffRepo)
        assert isinstance(date, RdiffTime)
        self._repo = weakref.proxy(repo)
        self.date = date

    @property
    def size(self):
        try:
            return self._repo.session_statistics[self.date].sourcefilesize
        except KeyError:
            return 0

    @property
    def has_errors(self):
        """Check if the history has errors."""
        try:
            return not self._repo.error_log[self.date].is_empty
        except KeyError:
            return False

    @property
    def errors(self):
        """Return error messages."""
        # Get error log entry
        try:
            entry = self._repo.error_log[self.date]
        except KeyError:
            return ""
        # Read the error log entry.
        try:
            return entry.read()
        except:
            return "Error reading log file: " + self._repo._decode(entry.name)

    @property
    def increment_size(self):
        try:
            return self._repo.session_statistics[self.date].incrementfilesize
        except KeyError:
            return 0


class IncrementEntry(object):

    """Instance of the class represent one increment at a specific date for one
    repository. The base repository is provided in the default constructor
    and the date is provided using an error_log.* file"""

    MISSING_SUFFIX = b".missing"

    SUFFIXES = [b".missing", b".snapshot.gz", b".snapshot",
                b".diff.gz", b".data.gz", b".data", b".dir", b".diff"]

    def __init__(self, parent, name):
        """Default constructor for an increment entry. User must provide the
            repository directory and an entry name. The entry name correspond
            to an error_log.* filename."""
        assert isinstance(parent, DirEntry) or isinstance(parent, RdiffRepo)
        assert isinstance(name, bytes)
        # Keep reference to the current path.
        if isinstance(parent, RdiffRepo):
            self.repo = parent
        else:
            self.repo = parent._repo
        # The given entry name may has quote character, replace them
        self.name = name
        # Calculate the date of the increment.
        self.date = self.repo._extract_date(self.name)

    @property
    def path(self):
        return os.path.join(self.repo._data_path, self.name)

    def _open(self):
        """Should be used to open the increment file. This method handle
        compressed vs not-compressed file."""
        if self._is_compressed:
            return popen(['zcat', self.path])
        return open(self.path, 'rb')

    @property
    def filename(self):
        filename = IncrementEntry._remove_suffix(self.name)
        return filename.rsplit(b".", 1)[0]

    @property
    def has_suffix(self):
        for suffix in IncrementEntry.SUFFIXES:
            if self.name.endswith(suffix):
                return True
        return False

    @property
    def _is_compressed(self):
        return self.name.endswith(b".gz")

    @property
    def isdir(self):
        return self.name.endswith(b".dir")

    @property
    def is_missing(self):
        """Check if the curent entry is a missing increment."""
        return self.name.endswith(self.MISSING_SUFFIX)

    @property
    def is_snapshot(self):
        """Check if the current entry is a snapshot increment."""
        return (self.name.endswith(b".snapshot.gz") or
                self.name.endswith(b".snapshot"))

    @staticmethod
    def _remove_suffix(filename):
        """ returns None if there was no suffix to remove. """
        for suffix in IncrementEntry.SUFFIXES:
            if filename.endswith(suffix):
                return filename[:-len(suffix)]
        return filename

    @property
    def is_empty(self):
        """
        Check if the increment entry is empty.
        """
        return os.path.getsize(self.path) == 0

    def __str__(self):
        return self.name


class FileStatisticsEntry(IncrementEntry):

    """
    Represent a single file_statistics.

    File Statistics contains different information related to each file of
    the backup. This class provide a simple and easy way to access this
    data.
    """

    def __init__(self, repo_path, name):
        IncrementEntry.__init__(self, repo_path, name)
        # check to ensure we have a file_statistics entry
        assert self.name.startswith(b"file_statistics.")
        assert self.name.endswith(b".data") or self.name.endswith(b".data.gz")

    def get_mirror_size(self, path):
        """Return the value of MirrorSize for the given file.
        path is the relative path from repo root."""
        try:
            return int(self._search(path)["mirror_size"])
        except:
            logger.warning("mirror size not found for [%r]", path, exc_info=1)
            return 0

    def get_source_size(self, path):
        """Return the value of SourceSize for the given file.
        path is the relative path from repo root."""
        try:
            return int(self._search(path)["source_size"])
        except:
            logger.warning("source size not found for [%r]", path, exc_info=1)
            return 0

    def _search(self, path):
        """
        This function search for a file entry in the file_statistics compress
        file. Since python gzip.open() seams to be 2 time slower, we directly use
        zlib library on python2.
        """
        logger.debug("read file_statistics [%r]", self.name)

        path += b' '

        with self._open() as f:
            for line in f:
                if not line.startswith(path):
                    continue
                break

        # Split the line into array
        data = line.rstrip(b'\r\n').rsplit(b' ', 4)
        # From array create an entry
        return {
            'changed': data[1],
            'source_size': data[2],
            'mirror_size': data[3],
            'increment_size': data[4]}


class SessionStatisticsEntry(IncrementEntry):

    """Represent a single session_statistics."""

    ATTRS = ['starttime', 'endtime', 'elapsedtime', 'sourcefiles', 'sourcefilesize',
             'mirrorfiles', 'mirrorfilesize', 'newfiles', 'newfilesize', 'deletedfiles',
             'deletedfilesize', 'changedfiles', 'changedsourcesize', 'changedmirrorsize',
             'incrementfiles', 'incrementfilesize', 'totaldestinationsizechange', 'errors']

    def __init__(self, repo_path, name):
        # check to ensure we have a file_statistics entry
        assert name.startswith(b"session_statistics")
        assert name.endswith(b".data") or name.endswith(b".data.gz")
        IncrementEntry.__init__(self, repo_path, name)

    def _load(self):
        """This method is used to read the session_statistics and create the
        appropriate structure to quickly get the data.

        File Statistics contains different information related to each file of
        the backup. This class provide a simple and easy way to access this
        data."""

        with self._open() as f:
            for line in f.readlines():
                # Skip comments
                if line.startswith(b"#"):
                    continue
                # Read the line into array
                line = line.rstrip(b'\r\n')
                data_line = line.split(b" ", 2)
                # Read line into tuple
                (key, value) = tuple(data_line)[0:2]
                if b'.' in value:
                    value = float(value)
                else:
                    value = int(value)
                setattr(self, key.lower().decode('ascii'), value)

    def __getattr__(self, name):
        """
        Intercept attribute getter to load the file.
        """
        if name in self.ATTRS:
            self._load()
        return self.__dict__[name]


class CurrentMirrorEntry(IncrementEntry):

    def extract_pid(self):
        """
        Return process ID from a current mirror marker, if any
        """
        with open(self.path, 'rb') as f:
            match = PID_RE.search(f.read())
        if not match:
            return None
        else:
            return int(match.group(1))


class LogEntry(IncrementEntry):

    def read(self):
        """Read the error file and return it's content. Raise exception if the
        file can't be read."""
        # To avoid opening empty file, check the file size first.
        if self.is_empty:
            return ""
        encoding = self.repo._encoding.name
        if self._is_compressed:
            return subprocess.check_output(['zcat', self.path], stdout=subprocess.PIPE, stderr=subprocess.STDOUT, encoding=encoding, errors='replace')
        with open(self.path, 'r', encoding=encoding, errors='replace') as f:
            return f.read()

    def tail(self, num=2000):
        """
        Tail content of the file. This is used for logs.
        """
        # To avoid opening empty file, check the file size first.
        if self.is_empty:
            return b''
        encoding = self.repo._encoding.name
        if self._is_compressed:
            return subprocess.check_output(['zcat', self.path, '|', 'tail', '-n', str(num)], stderr=subprocess.STDOUT, shell=True, encoding=encoding, errors='replace')
        return subprocess.check_output(['tail', '-n', str(num), self.path], stderr=subprocess.STDOUT, encoding=encoding, errors='replace')


class MetadataKeys:
    """
    Provide a view on metadata dict keys. See MetadataDict#keys()
    """

    def __init__(self, function, sequence):
        self._f = function
        self._sequence = sequence

    def __iter__(self):
        return map(self._f, self._sequence)

    def __getitem__(self, i):
        if isinstance(i, slice):
            return list(map(self._f, self._sequence[i]))
        else:
            return self._f(self._sequence[i])

    def __len__(self):
        return len(self._sequence)


class MetadataDict(object):
    """
    This is used to access repository metadata quickly in a pythonic way. It
    make an abstraction to access a range of increment entries using index and
    date while also supporting slice to get a range of entries.
    """

    def __init__(self, repo, prefix, cls):
        assert isinstance(repo, RdiffRepo)
        assert isinstance(prefix, bytes)
        assert hasattr(cls, '__call__')
        self._repo = repo
        self._prefix = prefix
        self._cls = cls

    @cached_property
    def _entries(self):
        return [e for e in self._repo._entries if e.startswith(self._prefix)]

    @cached_property
    def _idx_table(self):
        _extract_date = self._repo._extract_date
        return {_extract_date(e): idx for idx, e in enumerate(self._entries)}

    def __getitem__(self, key):
        if isinstance(key, RdiffTime):
            return self._cls(self._repo, self._entries[self._idx_table[key]])
        elif isinstance(key, slice):
            if isinstance(key.start, RdiffTime):
                idx = bisect.bisect_left(self.keys(), key.start)
                key = slice(idx, key.stop, key.step)
            if isinstance(key.stop, RdiffTime):
                idx = bisect.bisect_right(self.keys(), key.stop)
                key = slice(key.start, idx, key.step)
            return [self._cls(self._repo, e) for e in self._entries[key]]
        elif isinstance(key, int):
            return self._cls(self._repo, self._entries[key])

    def __iter__(self):
        for e in self._entries:
            yield self._cls(self._repo, e)

    def __len__(self):
        return len(self._entries)

    def keys(self):
        return MetadataKeys(self._repo._extract_date, self._entries)


class RdiffRepo(object):

    """Represent one rdiff-backup repository."""

    def __init__(self, user_root, path, encoding):
        if isinstance(user_root, str):
            user_root = os.fsencode(user_root)
        if isinstance(path, str):
            path = os.fsencode(path)
        assert isinstance(user_root, bytes)
        assert isinstance(path, bytes)
        assert encoding
        self._encoding = encodings.search_function(encoding)
        assert self._encoding
        self.path = path.strip(b"/")
        self.full_path = os.path.realpath(os.path.join(user_root, self.path))

        # The location of rdiff-backup-data directory.
        self._data_path = os.path.join(self.full_path, RDIFF_BACKUP_DATA)
        assert isinstance(self._data_path, bytes)
        self._increment_path = os.path.join(self._data_path, INCREMENTS)
        if not os.path.isdir(self._data_path):
            self._entries = []

        self.current_mirror = MetadataDict(
            self, b'current_mirror', CurrentMirrorEntry)
        self.error_log = MetadataDict(self, b'error_log', LogEntry)
        self.mirror_metadata = MetadataDict(
            self, b'mirror_metadata', IncrementEntry)
        self.file_statistics = MetadataDict(
            self, b'file_statistics', FileStatisticsEntry)
        self.session_statistics = MetadataDict(
            self, b'session_statistics', SessionStatisticsEntry)

    @property
    def backup_dates(self):
        """Return a list of dates when backup was executed. This list is
        sorted from old to new (ascending order). To identify dates,
        'mirror_metadata' file located in rdiff-backup-data are used."""
        return self.mirror_metadata.keys()

    @property
    def backup_log(self):
        """
        Return the location of the backup log.
        """
        return LogEntry(self, b'backup.log')

    def delete(self):
        """Delete the repository permanently."""

        # Try to change the permissions of the file or directory to delete
        # them.
        def handle_error(func, path, exc_info):
            if exc_info[0] == PermissionError:
                # Parent directory must allow rwx
                if not os.access(os.path.dirname(path), os.W_OK | os.R_OK | os.X_OK):
                    os.chmod(os.path.dirname(path), 0o0700)
                if not os.access(path, os.W_OK | os.R_OK):
                    os.chmod(path, 0o0600)
                if os.path.isdir(path):
                    return shutil.rmtree(path, onerror=handle_error)
                else:
                    return os.unlink(path)
            raise

        try:
            shutil.rmtree(self.full_path, onerror=handle_error)
        except:
            logger.warn('fail to delete repo', exc_info=1)

    @property
    def display_name(self):
        """Return the most human representation of the repository name."""
        # NOTE : path may be empty, so return a simple string.
        if self.path:
            return self._decode(self.unquote(self.path))
        return self._decode(self.unquote(os.path.basename(self.full_path)))

    def _decode(self, value, errors='replace'):
        """Used to decode a repository path into unicode."""
        assert isinstance(value, bytes)
        return self._encoding.decode(value, errors)[0]

    @cached_property
    def _entries(self):
        return sorted(os.listdir(self._data_path))

    def _extract_date(self, filename):
        """
        Extract date from rdiff-backup filenames.
        """
        # Remove suffix from filename
        filename = IncrementEntry._remove_suffix(filename)
        # Remove prefix from filename
        date_string = filename.rsplit(b".", 1)[-1]
        # Unquote string
        date_string = self.unquote(date_string)
        try:
            return RdiffTime(date_string.decode())
        except:
            logger.warn('fail to parse date [%r]', date_string, exc_info=1)
            return None

    def _get_increment_entries(self, path):
        """
        Get the increment entries for the current path. This path is located
        under rdiff-backup-data/increments.
        """
        # Compute increment directory location.
        p = os.path.join(self._increment_path, path.strip(b'/'))
        assert p.startswith(self.full_path)

        # Check if increment directory exists. The path may not exists if
        # the folder always exists and never changed.
        if not os.access(p, os.F_OK):
            return []

        # List content of the increment directory.
        # Ignore sub-directories.
        entries = [
            IncrementEntry(self, x)
            for x in os.listdir(p)
            if not os.path.isdir(os.path.join(p, x))]
        return entries

    def get_path(self, path):
        """Return a new instance of DirEntry to represent the given path."""
        if isinstance(path, str):
            path = os.fsencode(path)
        path = os.path.normpath(path.strip(b"/"))

        # Get if the path request is the root path.
        if path == b'.':
            return DirEntry(self, b'', True, [])

        # Remove access to rdiff-backup-data directory.
        if path.startswith(RDIFF_BACKUP_DATA):
            raise DoesNotExistError(path)

        # Resolve symlink to make sure we do not leave the repo
        # and to break symlink loops.
        p = os.path.realpath(os.path.join(self.full_path, path))
        if not p.startswith(self.full_path):
            raise SymLinkAccessDeniedError(path)
        path = os.path.relpath(p, self.full_path)

        # Check if path exists or has increment. If not raise an exception.
        exists = os.path.exists(p)
        fn = os.path.basename(p)
        increments = [e for e in self._get_increment_entries(os.path.dirname(path))
                      if e.filename == fn]
        if not exists and not increments:
            logger.error("path [%r] doesn't exists", path)
            raise DoesNotExistError(path)

        # Create a directory entry.
        return DirEntry(self, path, exists, increments)

    @property
    def last_backup_date(self):
        """Return the last known backup dates."""
        try:
            if len(self.current_mirror) > 0:
                return self.current_mirror[-1].date
            return None
        except (PermissionError, FileNotFoundError):
            return None

    def remove_older(self, remove_older_than):
        logger.info("execute rdiff-backup --force --remove-older-than=%sD %r",
                    remove_older_than, self.full_path)
        subprocess.call([b'rdiff-backup', b'--force', b'--remove-older-than=' +
                         str(remove_older_than).encode(encoding='latin1') + b'D', self.full_path])

    @property
    def restore_log(self):
        """
        Return the location of the restore log.
        """
        return LogEntry(self, b'restore.log')

    @cached_property
    def status(self):
        """Check if a backup is in progress for the current repo."""

        # Read content of the file and check if pid still exists
        try:
            # Check if the repository exists.
            # Make sure repoRoot is a valid rdiff-backup repository
            if not os.path.isdir(self._data_path):
                self._entries = []
                return ('failed', _('The repository cannot be found or is badly damaged.'))

            for current_mirror in self.current_mirror:
                pid = current_mirror.extract_pid()
                try:
                    p = psutil.Process(pid)
                    if any('rdiff-backup' in c for c in p.cmdline()):
                        return ('in_progress', _('A backup is currently in progress to this repository.'))
                except psutil.NoSuchProcess:
                    logger.debug('pid [%s] does not exists', pid)
                    pass

            # If multiple current_mirror file exists and none of them are associated to a PID, this mean the last backup was interrupted.
            # Also, if the last backup date is undefined, this mean the first
            # initial backup was interrupted.
            if len(self.current_mirror) > 1 or len(self.current_mirror) == 0:
                self._status = ('interrupted', _(
                    'The previous backup seams to have failed.'))
                return self._status

        except PermissionError:
            self._entries = []
            logger.warn('error reading current_mirror files', exc_info=1)
            return ('failed', _("Permissions denied. Contact administrator to check repository's permissions."))

        return ('ok', '')

    def unquote(self, name):
        """Remove quote from the given name."""
        assert isinstance(name, bytes)

        # This function just gives back the original text if it can decode it
        def unquoted_char(match):
            """For each ;000 return the corresponding byte."""
            if not len(match.group()) == 4:
                return match.group
            try:
                return bytes([int(match.group()[1:])])
            except:
                return match.group

        # Remove quote using regex
        return re.sub(b";[0-9]{3}", unquoted_char, name, re.S)

    def __str__(self):
        return "%r" % (self.full_path,)
