"""
Extract text from the text-code stream and comment it.

Supports three modes of normalization and commenting:

    1. Don't add any comments
    2. Add comments
    3. Remove text, leave code only

Since several operations are quite expensice,
actively uses caching.

Exported functions:

    beautify(text, lang, options)
    code_blocks(text)
"""

from gevent.monkey import patch_all
from gevent.subprocess import Popen
patch_all()

# pylint: disable=wrong-import-position,wrong-import-order
import sys
import os
import textwrap
import hashlib
import re

from itertools import groupby, chain
from tempfile import NamedTemporaryFile

import redis

MYDIR = os.path.abspath(os.path.dirname(os.path.dirname('__file__')))
sys.path.append("%s/lib/" % MYDIR)
from languages_data import VIM_NAME
from globals import PATH_VIM_ENVIRONMENT
# pylint: enable=wrong-import-position,wrong-import-order

REDIS = redis.StrictRedis(host='localhost', port=6379, db=1)
FNULL = open(os.devnull, 'w')

def _language_name(name):
    return VIM_NAME.get(name, name)

def _cleanup_lines(lines):
    """
    Cleanup `lines` a little bit: remove empty lines at the beginning
    and at the end; remove to much empty lines in between.
    """

    if lines == []:
        return lines

    # remove empty lines from the beginning
    start = 0
    while start < len(lines) and lines[start].strip() == '':
        start += 1
    lines = lines[start:]
    if lines == []:
        return lines

    # remove empty lines from the end
    end = len(lines) - 1
    while end >= 0 and lines[end].strip() == '':
        end -= 1
    lines = lines[:end+1]
    if lines == []:
        return lines

    # remove repeating empty lines
    lines = list(chain.from_iterable(
        [(list(x[1]) if x[0] else [''])
         for x in groupby(lines, key=lambda x: x.strip() != '')]))

    return lines


def _classify_lines(lines):
    """
    Classify each line and say which of them
    are text (0) and which of them are code (1).

    A line is considered to be code,
    if it starts with four spaces.

    A line is considerer to be text if it is not
    empty and is not code.

    If line is empty, it is considered to be
    code if it surrounded but two other code lines
    (or if it is the first/last line and it has
    code on the other side.
    """

    def _line_type(line):
        if line.strip() == '':
            return -1

        # some line may start with spaces but still be not code.
        # we need some heuristics here, but for the moment just
        # whitelist such cases:
        if line.strip().startswith('* ') or re.match(r'[0-9]+\.', line.strip()):
            return 0

        if line.startswith('   '):
            return 1
        return 0

    line_types = [_line_type(line) for line in lines]

    # pass 2:
    # adding empty code lines to the code
    for i in range(len(line_types) - 1):
        if line_types[i] == 1 and line_types[i+1] == -1:
            line_types[i+1] = -2
            changed = True

    for i in range(len(line_types) - 1)[::-1]:
        if line_types[i] == -1 and line_types[i+1] == 1:
            line_types[i] = -2
            changed = True
    line_types = [1 if x == -2 else x for x in line_types]

    # pass 3:
    # fixing undefined line types (-1)
    changed = True
    while changed:
        changed = False

        # changing all lines types that are near the text

        for i in range(len(line_types) - 1):
            if line_types[i] == 0 and line_types[i+1] == -1:
                line_types[i+1] = 0
                changed = True

        for i in range(len(line_types) - 1)[::-1]:
            if line_types[i] == -1 and line_types[i+1] == 0:
                line_types[i] = 0
                changed = True

    # everything what is still undefined, change to 1
    line_types = [1 if x == -1 else x for x in line_types]
    return line_types

def _wrap_lines(lines_classes, unindent_code=False):
    """
    Wrap classified lines. Add the splitted lines to the stream.
    If `unindent_code` is True, remove leading four spaces.
    """

    def _unindent_code(line, shift=0):
        #if line.startswith('    '):
        #    return line[4:]

        if shift == -1 and line != '':
            return ' ' + line

        if shift > 0:
            if line.startswith(' '*shift):
                return line[shift:]

        return line

    result = []
    for line_tuple in lines_classes:
        if line_tuple[0] == 1:
            if unindent_code:
                shift = 3 if unindent_code is True else unindent_code
            else:
                shift = -1
            result.append((line_tuple[0], _unindent_code(line_tuple[1], shift=shift)))
        else:
            if line_tuple[1].strip() == "":
                result.append((line_tuple[0], ""))
            for line in textwrap.fill(line_tuple[1]).splitlines():
                result.append((line_tuple[0], line))

    return result

def _run_vim_script(script_lines, text_lines):
    """
    Apply `script_lines` to `lines_classes`
    and returns the result
    """

    script_vim = NamedTemporaryFile(delete=True)
    textfile = NamedTemporaryFile(delete=True)

    open(script_vim.name, "w").write("\n".join(script_lines))
    open(textfile.name, "w").write("\n".join(text_lines))

    script_vim.file.close()
    textfile.file.close()

    my_env = os.environ.copy()
    my_env['HOME'] = PATH_VIM_ENVIRONMENT

    cmd = ["script", "-q", "-c",
           "vim -S %s %s" % (script_vim.name, textfile.name)]
    Popen(cmd, shell=False, stdout=FNULL, stderr=FNULL, env=my_env).communicate()

    return open(textfile.name, "r").read()

def _commenting_script(lines_blocks, filetype):
    script_lines = []
    block_start = 1
    for block in lines_blocks:
        lines = list(block[1])

        block_end = block_start + len(lines)-1

        if block[0] == 0:
            comment_type = 'sexy'
            if block_end - block_start < 1 or filetype == 'ruby':
                comment_type = 'comment'

            script_lines.insert(0, "%s,%s call NERDComment(1, '%s')"
                                % (block_start, block_end, comment_type))
            script_lines.insert(0, "%s,%s call NERDComment(1, 'uncomment')"
                                % (block_start, block_end))

        block_start = block_end + 1

    script_lines.insert(0, "set ft=%s" % _language_name(filetype))
    script_lines.append("wq")

    return script_lines

def _beautify(text, filetype, add_comments=False, remove_text=False):
    """
    Main function that actually does the whole beautification job.
    """

    # We shift the code if and only if we either convert the text into comments
    # or remove the text completely. Otherwise the code has to remain aligned
    unindent_code = add_comments or remove_text
    print unindent_code

    lines = [x.rstrip('\n') for x in text.splitlines()]
    lines = _cleanup_lines(lines)
    lines_classes = zip(_classify_lines(lines), lines)
    lines_classes = _wrap_lines(lines_classes, unindent_code=unindent_code)
    #for x,y in lines_classes:
    #   print "%s: %s" % (x, y)

    if remove_text:
        lines = [line[1] for line in lines_classes if line[0] == 1]
        lines = _cleanup_lines(lines)
        output = "\n".join(lines)
        if not output.endswith('\n'):
            output += "\n"
    elif not add_comments:
        output = "\n".join(line[1] for line in lines_classes)
    else:
        lines_blocks = groupby(lines_classes, key=lambda x: x[0])
        script_lines = _commenting_script(lines_blocks, filetype)
        output = _run_vim_script(
            script_lines,
            [line for (_, line) in lines_classes])

    return output

def code_blocks(text, wrap_lines=False, unindent_code=False):
    """
    Split `text` into blocks of text and code.
    Return list of tuples TYPE, TEXT
    """
    lines = [x.rstrip('\n') for x in text.splitlines()]
    lines_classes = zip(_classify_lines(lines), lines)

    if wrap_lines:
        lines_classes = _wrap_lines(lines_classes, unindent_code=unindent_code)

    lines_blocks = groupby(lines_classes, key=lambda x: x[0])
    answer = [(x[0], "\n".join([y[1] for y in x[1]])+"\n") for x in lines_blocks]
    return answer


def beautify(text, lang, options):
    """
    Process input `text` according to the specified `mode`.
    Adds comments if needed, according to the `lang` rules.
    Caches the results.
    The whole work (except caching) is done by _beautify().
    """

    options = options or {}
    beauty_options = dict((k, v) for k, v in options.items() if k in
                          ['add_comments', 'remove_text'])

    mode = ''
    if beauty_options.get('add_comments'):
        mode += 'c'
    if beauty_options.get('remove_text'):
        mode += 'q'

    if beauty_options == {}:
        # if mode is unknown, just don't transform the text at all
        return text

    digest = "t:%s:%s:%s" % (hashlib.md5(text).hexdigest(), lang, mode)
    answer = REDIS.get(digest)
    if answer:
        return answer

    answer = _beautify(text, lang, **beauty_options)

    REDIS.set(digest, answer)
    return answer

def __main__():
    text = sys.stdin.read()
    filetype = sys.argv[1]
    options = {
        "": {},
        "c": dict(add_comments=True),
        "C": dict(add_comments=False),
        "q": dict(remove_text=True),
    }[sys.argv[2]]
    result = beautify(text, filetype, options)
    sys.stdout.write(result)

if __name__ == '__main__':
    __main__()
