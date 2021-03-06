# -*- coding: utf-8 -*-
# Copyright 2015 Yelp and Contributors
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
# http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.
"""Parse "task" logs, which are the syslog and stderr for each individual
task and typically appear in the userlogs/ directory."""
import posixpath
import re

from mrjob.util import file_ext
from .ids import _add_implied_task_id
from .ids import _sort_by_recency
from .ids import _to_job_id
from .log4j import _parse_hadoop_log4j_records
from .wrap import _cat_log
from .wrap import _ls_logs


# Match a java exception, possibly preceded by 'PipeMapRed failed!', etc.
# use this with search()
_JAVA_TRACEBACK_RE = re.compile(
    r'$\s+at .*\((.*\.java:\d+|Native Method)\)$',
    re.MULTILINE)

# this seems to only happen for S3. Not sure if this happens in YARN
_OPENING_FOR_READING_RE = re.compile(
    r"^Opening '(?P<path>.*?)' for reading$")

# what syslog paths look like pre-YARN
_PRE_YARN_TASK_SYSLOG_PATH_RE = re.compile(
    r'^(?P<prefix>.*?/)'
    r'(?P<attempt_id>attempt_(?P<timestamp>\d+)_(?P<step_num>\d+)_'
    r'(?P<task_type>[mr])_(?P<task_num>\d+)_'
    r'(?P<attempt_num>\d+))/'
    r'syslog(?P<suffix>\.\w+)?')

# message telling us about a (input) split. Looks like this:
#
# Processing split: hdfs://ddf64167693a:9000/path/to/bootstrap.sh:0+335
_YARN_INPUT_SPLIT_RE = re.compile(
    r'^Processing split:\s+(?P<path>.*)'
    r':(?P<start_line>\d+)\+(?P<num_lines>\d+)$')

# what syslog paths look like on YARN
_YARN_TASK_SYSLOG_PATH_RE = re.compile(
    r'^(?P<prefix>.*?/)'
    r'(?P<application_id>application_\d+_\d{4})/'
    r'(?P<container_id>container(_\d+)+)/'
    r'syslog(?P<suffix>\.\w+)?')


def _ls_task_syslogs(fs, log_dir_stream, application_id=None, job_id=None):
    """Yield matching syslogs, optionally filtering by application_id
    or job_id."""
    return _ls_logs(fs, log_dir_stream, _match_task_syslog_path,
                    application_id=application_id,
                    job_id=job_id)


# TODO: will need to filter by step_num on EMR
def _match_task_syslog_path(path, application_id=None, job_id=None):
    """Is this the path/URI of a task syslog?

    If so, return a dictionary containing application_id and container_id
    (on YARN) or attempt_id (on pre-YARN Hadoop). Otherwise, return None

    Optionally, filter by application_id (YARN) or job_id (pre-YARN).
    """
    m = _PRE_YARN_TASK_SYSLOG_PATH_RE.match(path)
    if m:
        if job_id and job_id != _to_job_id(m.group('attempt_id')):
            return None  # matches, but wrong job_id
        return dict(
            attempt_id=m.group('attempt_id'))

    m = _YARN_TASK_SYSLOG_PATH_RE.match(path)
    if m:
        if application_id and application_id != m.group('application_id'):
            return None  # matches, but wrong application_id
        return dict(
            application_id=m.group('application_id'),
            container_id=m.group('container_id'))

    return None


def _interpret_task_logs(fs, matches, partial=True):
    """Look for errors in task syslog/stderr.

    If *partial* is true (the default), work backwards from the most
    recent log file and stop when we find the first error.

    Returns a dictionary possibly containing the key 'errors', which
    is a dict containing:

    hadoop_error:
        message: string containing error message and Java exception
        num_lines: number of lines in syslog this takes up
        path: syslog we read this error from
        start_line: where in syslog exception starts (0-indexed)
    split: (optional)
        path: URI of input file task was processing
        num_lines: (optional) number of lines in split
        start_line: (optional) first line of split (0-indexed)
    task_error:
        message: command and error message from task, as a string
        num_lines: number of lines in stderr this takes up
        path: stderr we read this from
        start_line: where in stderr error message starts (0-indexed)

    In addition, if *partial* is set to true (and we found an error),
    this dictionary will contain the key *partial*, set to True.
    """
    result = {}

    if partial:
        matches = _sort_by_recency(matches)

    for match in matches:
        syslog_path = match['path']

        # get hadoop_error and possibly split from syslog
        error = _parse_task_syslog(_cat_log(fs, syslog_path))
        if not error.get('hadoop_error'):
            continue
        error['hadoop_error']['path'] = syslog_path

        # path in IDs we learned from path
        for id_key in 'attempt_id', 'container_id', 'type_id':
            if id_key in match:
                error[id_key] = match[id_key]

        _add_implied_task_id(error)

        # look for task_error in stderr, if it exists
        stderr_path = _syslog_to_stderr_path(syslog_path)
        task_error = _parse_task_stderr(_cat_log(fs, stderr_path))

        if task_error:
            task_error['path'] = stderr_path
            error['task_error'] = task_error

        result.setdefault('errors', [])
        result['errors'].append(error)

        if partial:
            result['partial'] = True
            break

    return result


def _syslog_to_stderr_path(path):
    """Get the path/uri of the stderr log corresponding to the given syslog.

    If the syslog is gzipped (/path/to/syslog.gz), we'll expect
    stderr to be gzipped too (/path/to/stderr.gz).
    """
    stem, filename = posixpath.split(path)
    return posixpath.join(stem, 'stderr' + file_ext(filename))


def _parse_task_syslog(lines):
    """Parse an error out of a syslog file.

    Returns a dict, possibly containing the following keys:

    hadoop_error:
        message: string containing error message and Java exception
        num_lines: number of lines in syslog this takes up
        start_line: where in syslog exception starts (0-indexed)
    split: (optional)
        path: URI of input file task was processing
        num_lines: (optional) number of lines in split
        start_line: (optional) first line of split (0-indexed)
    """
    result = {}


    split = None
    hadoop_error = None

    for record in _parse_hadoop_log4j_records(lines):
        message = record['message']

        m = _OPENING_FOR_READING_RE.match(message)
        if m:
            result['split'] = dict(path=m.group('path'))
            continue

        m = _YARN_INPUT_SPLIT_RE.match(message)
        if m:
            result['split'] = dict(
                path=m.group('path'),
                start_line=int(m.group('start_line')),
                num_lines=int(m.group('num_lines')))
            continue

        m = _JAVA_TRACEBACK_RE.search(message)
        if m:
            result['hadoop_error'] = dict(
                message=message,
                num_lines=record['num_lines'],
                start_line=record['start_line'],
            )
            break  # nothing to do once we've found the error

    return result


def _parse_task_stderr(lines):
    """Attempt to explain any error in task stderr, be it a Python
    exception or a problem with a setup command (see #1203).

    Currently this only works with tasks run with the setup wrapper script;
    it looks for '+ ' followed by a command line, and then the command's
    stderr.

    Either returns None or a task error dictionary with the following keys:

    message: a string (e.g. Python command line followed by Python traceback)
    start_line: where in lines message appears (0-indexed)
    num_lines: how may lines the message takes up
    """
    task_error = None

    for line_num, line in enumerate(lines):
        line = line.rstrip('\r\n')

        if line.startswith('+ '):
            task_error = dict(
                message=line,
                start_line=line_num)
        elif task_error:
            # explain what wrong!
            task_error['message'] += '\n' + line

    if task_error:
        task_error['num_lines'] = line_num + 1 - task_error['start_line']
        return task_error
    else:
        return None
