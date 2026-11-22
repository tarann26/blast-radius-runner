"""State guard: a patch that makes sqlglot write to state shared between tests (a class-level
setting, a module-level table, an object that outlives a test) can break tests that run later,
in other files, while every test passes when run on its own. For such a commit every candidate
test is reported, and for dialect code every dialect test too, whatever the runs found."""
from __future__ import annotations
import re
MUTATORS = '(?:pop|update|append|add|clear|setdefault|extend|insert|remove|discard|popitem)'
WRITE = re.compile('type\\(\\s*self\\s*\\)\\.\\w+\\s*=(?!=)|\\b(?:self|cls)\\.[A-Z][A-Z0-9_]*\\s*\\[[^\\]]*\\]\\s*=(?!=)|\\b(?:self|cls|type\\(\\s*self\\s*\\))\\.[A-Z_][A-Z0-9_]*\\.' + MUTATORS + '\\(|\\b[A-Z][A-Z0-9_]{2,}\\.(?:' + MUTATORS[3:-1] + '|set)\\(|\\b[A-Z]\\w*\\.\\w+\\.' + MUTATORS + '\\(|\\b[A-Z][A-Z0-9_]{2,}\\s*\\[[^\\]]+\\]\\s*=(?!=)|^\\s*(?:global|nonlocal)\\s|\\bsetattr\\(\\s*(?:cls|type\\(|[A-Z])')
COPY = re.compile('\\.copy\\(\\s*\\)|deepcopy\\(|\\bcopy\\s*=\\s*True\\b|\\b(?:set|dict|list|tuple|frozenset)\\(\\s*(?:cls|self)\\.')
DIALECT_CODE = ('sqlglot/dialects/', 'sqlglot/parsers/', 'sqlglot/generators/', 'sqlglot/tokenizer', 'sqlglot/tokens.py', 'sqlglot/parser.py', 'sqlglot/generator.py')

def flagged_files(patch_text: str) -> set[str]:
    out: set[str] = set()
    current = None
    copies: dict[str, list[int]] = {}
    for line in patch_text.split('\n'):
        if line.startswith('+++ '):
            path = line[4:].strip()
            current = path[2:] if path.startswith('b/') else path
            continue
        if line.startswith('--- ') or current is None:
            continue
        if not (current.startswith('sqlglot/') and current.endswith('.py')):
            continue
        if line.startswith('+'):
            body = line[1:]
            if body[:4] == '    ' and WRITE.search(body):
                out.add(current)
            copies.setdefault(current, [0, 0])[1] += len(COPY.findall(body))
        elif line.startswith('-'):
            copies.setdefault(current, [0, 0])[0] += len(COPY.findall(line[1:]))
    out.update((f for f, (old, new) in copies.items() if new < old))
    return out
ANN = re.compile('^([-+])\\s+([A-Za-z_]\\w*)\\s*:\\s*[^=]+=')
PLAIN = re.compile('^([-+])\\s+([A-Za-z_]\\w*)\\s*=(?!=)')

def annotation_edit(patch_text: str) -> bool:
    seen = {'-ann': set(), '+ann': set(), '-plain': set(), '+plain': set()}
    for line in patch_text.split('\n'):
        if line.startswith(('+++', '---')):
            continue
        m = ANN.match(line)
        if m:
            seen[m.group(1) + 'ann'].add(m.group(2))
            continue
        m = PLAIN.match(line)
        if m:
            seen[m.group(1) + 'plain'].add(m.group(2))
    return bool(seen['-ann'] & seen['+plain'] or seen['+ann'] & seen['-plain'])
MODULE_SKIP = re.compile('^(?:def |class |async def |@|import |from |#|"""|\'\'\'|\\)|\\]|\\}|pass\\s*$|if (?:t\\.)?TYPE_CHECKING|$)')

def module_level_edit(patch_text: str) -> bool:
    current = None
    for line in patch_text.split('\n'):
        if line.startswith('+++ '):
            path = line[4:].strip()
            current = path[2:] if path.startswith('b/') else path
            continue
        if line.startswith('--- ') or current is None:
            continue
        if not (current.startswith('sqlglot/') and current.endswith('.py')):
            continue
        if line[:1] in '+-':
            body = line[1:]
            if body and (not body[0].isspace()) and (not MODULE_SKIP.match(body)):
                return True
    return False

def extra_tests(patch_text: str, candidates, num_to_test: dict) -> set[int]:
    files = flagged_files(patch_text)
    if not files:
        if annotation_edit(patch_text) or module_level_edit(patch_text):
            return set(candidates or ())
        return set()
    extra = set(candidates or ())
    if any((f.startswith(DIALECT_CODE) for f in files)):
        extra.update((n for n, t in num_to_test.items() if t['file'].startswith('tests/dialects/')))
    return extra
