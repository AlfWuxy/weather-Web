#!/usr/bin/env python3
"""重置 admin 密码"""
import argparse
import getpass
import os
import sqlite3
import sys
from pathlib import Path
from werkzeug.security import generate_password_hash

ROOT_DIR = Path(__file__).resolve().parents[1]
if str(ROOT_DIR) not in sys.path:
    sys.path.insert(0, str(ROOT_DIR))

from core.config import resolve_database_uri, resolve_sqlite_db_path


_GENERIC_CLI_ERROR = '参数错误，请检查命令行选项'
_PASSWORD_STDIN_INLINE_ERROR = '`--password-stdin` 不接受内联参数值'


class _PasswordStdinInlineError(Exception):
    """拒绝 --password-stdin=值。异常文本是固定句子，不含用户传入的值。"""

    def __init__(self):
        super().__init__(_PASSWORD_STDIN_INLINE_ERROR)


class _DisabledPasswordAction(argparse.Action):
    """命令行一旦出现 --password 即拒绝，且不回显传入值。"""

    def __call__(self, parser, namespace, values, option_string=None):
        del parser, namespace, values, option_string
        raise RuntimeError(
            '已禁用命令行 --password 参数以避免进程参数泄露密码；请直接交互运行或使用 --password-stdin。'
        )


class _PasswordStdinAction(argparse.Action):
    """--password-stdin 只能作为开关。内联值直接丢弃，不进入异常文本。"""

    def __init__(self, option_strings, dest, nargs=None, const=None, default=None, type=None, **kwargs):
        del nargs, const, default, type
        super().__init__(
            option_strings,
            dest,
            nargs=0,
            const=True,
            default=False,
            **kwargs,
        )

    def __call__(self, parser, namespace, values, option_string=None):
        del parser, option_string
        if isinstance(values, str):
            del values
            raise _PasswordStdinInlineError()
        setattr(namespace, self.dest, True)


def _write_generic_cli_error():
    print(_GENERIC_CLI_ERROR, file=sys.stderr)


def _detach_exception_context(exc):
    exc.__context__ = None
    exc.__cause__ = None
    exc.__suppress_context__ = True


def _reject_inline_password_stdin(args):
    """在 argparse 拼出错误句之前拦住 --password-stdin=值。"""
    for token in args:
        if isinstance(token, str) and token.startswith('--password-stdin='):
            raise _PasswordStdinInlineError()


class _SafeArgumentParser(argparse.ArgumentParser):
    """解析失败时只输出固定中文，不回显 argv 中的值。"""

    def __init__(self, *args, **kwargs):
        kwargs['allow_abbrev'] = False
        kwargs['exit_on_error'] = False
        super().__init__(*args, **kwargs)

    def parse_args(self, args=None, namespace=None):
        if args is None:
            args = sys.argv[1:]
        args = list(args)
        _reject_inline_password_stdin(args)
        invalid = False
        try:
            return super().parse_args(args, namespace)
        except argparse.ArgumentError:
            invalid = True
        except SystemExit as exc:
            _detach_exception_context(exc)
            code = exc.code
            if isinstance(code, (str, bytes)) and code:
                invalid = True
            else:
                raise
        if invalid:
            # 在 except 之外退出，避免把含原始参数的 ArgumentError 挂到上下文上。
            self.error('')

    def error(self, message):
        # message 可能含 unrecognized arguments / ignored explicit argument 及原始值。
        del message
        _write_generic_cli_error()
        raise SystemExit(2)


def _load_database_uri():
    env_uri = (os.getenv('DATABASE_URI') or '').strip()
    if env_uri:
        return env_uri
    env_path = Path(__file__).resolve().parents[1] / '.env'
    if not env_path.exists():
        return ''
    for line in env_path.read_text(encoding='utf-8').splitlines():
        line = line.strip()
        if not line or line.startswith('#'):
            continue
        if not line.startswith('DATABASE_URI='):
            continue
        value = line.split('=', 1)[1].strip()
        value = value.split('#', 1)[0].strip()
        if value and value[0] in ('"', "'") and value[-1] == value[0]:
            value = value[1:-1]
        return value
    return ''


def _resolve_db_path(cli_db_path):
    if cli_db_path:
        return Path(cli_db_path).expanduser().resolve()

    db_uri = _load_database_uri() or resolve_database_uri()
    resolved = resolve_sqlite_db_path(db_uri, ROOT_DIR)
    if resolved is not None:
        return resolved

    return (ROOT_DIR / 'instance' / 'health_weather.db').resolve()


def _strip_stdin_line_ending(text):
    if text.endswith('\r\n'):
        return text[:-2]
    if text.endswith('\n'):
        return text[:-1]
    if text.endswith('\r'):
        return text[:-1]
    return text


def _read_password_from_stdin():
    if sys.stdin.isatty():
        raise RuntimeError('标准输入是交互终端，请去掉 --password-stdin，直接运行脚本并交互输入密码。')
    return _strip_stdin_line_ending(sys.stdin.readline())


def _prompt_new_password():
    first = getpass.getpass('请输入新的 admin 密码: ')
    second = getpass.getpass('请再次输入新的 admin 密码: ')
    if not first:
        raise RuntimeError('新密码不能为空。')
    if first != second:
        raise RuntimeError('两次输入的密码不一致。')
    return first


def _resolve_new_password(password_stdin):
    env_password = (os.getenv('NEW_ADMIN_PASSWORD') or '').strip()
    if password_stdin and env_password:
        raise RuntimeError('不能同时使用非空的 NEW_ADMIN_PASSWORD 与 --password-stdin，请只选择一种方式。')
    if password_stdin:
        return _read_password_from_stdin()
    if env_password:
        return env_password
    if not sys.stdin.isatty():
        raise RuntimeError('请通过 --password-stdin 或 NEW_ADMIN_PASSWORD 提供新密码。')
    return _prompt_new_password()


def _api_tokens_table_exists(cursor):
    cursor.execute(
        "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = ?",
        ('api_tokens',),
    )
    return cursor.fetchone() is not None


def main():
    parser = _SafeArgumentParser(description='重置 admin 密码', allow_abbrev=False)
    parser.add_argument(
        '--password',
        nargs='?',
        action=_DisabledPasswordAction,
        help=argparse.SUPPRESS,
    )
    parser.add_argument(
        '--password-stdin',
        action=_PasswordStdinAction,
        help='从标准输入读取一行新密码。交互终端请直接运行脚本。',
    )
    parser.add_argument('--db-path', help='显式指定 sqlite 数据库路径。')
    try:
        args = parser.parse_args()
    except _PasswordStdinInlineError as exc:
        print(str(exc), file=sys.stderr)
        return 2
    except argparse.ArgumentError:
        _write_generic_cli_error()
        return 2
    except SystemExit as exc:
        _detach_exception_context(exc)
        code = exc.code
        if isinstance(code, (str, bytes)) and code:
            _write_generic_cli_error()
            return 2
        raise

    new_password = _resolve_new_password(args.password_stdin)
    if str(ROOT_DIR) not in sys.path:
        sys.path.insert(0, str(ROOT_DIR))
    from utils.validators import validate_password
    valid, result = validate_password(new_password)
    if not valid:
        raise RuntimeError(result)

    db_path = _resolve_db_path(args.db_path)
    if not db_path.exists():
        raise RuntimeError(f'数据库文件不存在: {db_path}')

    try:
        conn = sqlite3.connect(db_path)
    except sqlite3.OperationalError as exc:
        raise RuntimeError(f'无法打开数据库文件: {db_path} ({exc})') from exc
    try:
        cursor = conn.cursor()
        new_hash = generate_password_hash(new_password)
        try:
            cursor.execute('UPDATE users SET password_hash = ? WHERE username = ?', (new_hash, 'admin'))
        except sqlite3.OperationalError as exc:
            conn.rollback()
            raise RuntimeError(f'更新 admin 密码失败，请检查 users 表是否存在: {exc}') from exc
        if cursor.rowcount == 0:
            conn.rollback()
            raise RuntimeError('未找到 admin 账户，密码未更新。')
        updated_rows = cursor.rowcount
        try:
            if _api_tokens_table_exists(cursor):
                cursor.execute(
                    'UPDATE api_tokens SET revoked_at = CURRENT_TIMESTAMP WHERE user_id = (SELECT id FROM users WHERE username = ?) AND revoked_at IS NULL',
                    ('admin',),
                )
            conn.commit()
        except sqlite3.OperationalError as exc:
            conn.rollback()
            raise RuntimeError(f'更新 admin 密码失败，请检查 users 表是否存在: {exc}') from exc
        print(f'已重置 admin 密码, 更新行数: {updated_rows}')
    finally:
        conn.close()

    return 0


if __name__ == '__main__':
    raise SystemExit(main())
