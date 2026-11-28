"""install.sh: хелперы, разбор ответа Telegram, обновление с откатом, конфиг nginx (без сети, docker и systemd)."""
import os
import shutil
import subprocess
from pathlib import Path

import pytest

SCRIPT = Path(__file__).resolve().parent.parent / "install.sh"
pytestmark = pytest.mark.skipif(shutil.which("bash") is None or shutil.which("git") is None,
                                reason="нужны bash и git")


def bash(code: str, env=None, cwd=None):
    e = {**os.environ, "CRYMODER_SOURCED": "1", "GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@t",
         "GIT_COMMITTER_NAME": "t", "GIT_COMMITTER_EMAIL": "t@t", **(env or {})}
    return subprocess.run(["bash", "-c", f'source "{SCRIPT}"\n{code}'], capture_output=True, text=True, env=e,
                          cwd=cwd, timeout=60)


def test_syntax():
    assert subprocess.run(["bash", "-n", str(SCRIPT)]).returncode == 0


def test_kv_helpers(tmp_path):
    f = tmp_path / ".env"
    f.write_text("A=1\nBOT_TOKEN=123456:replace_me\n# comment\nC=3\n")
    r = bash(f'''kv_set "{f}" BOT_TOKEN "9:AA/b&c|d-_x"; kv_set "{f}" NEW "v"; kv_set "{f}" A 2
echo "[$(kv_get "{f}" BOT_TOKEN)]" "[$(kv_get "{f}" MISSING)]" "[$(kv_get "{tmp_path}/nofile" X)]"''')
    assert r.returncode == 0, r.stderr
    assert "[9:AA/b&c|d-_x] [] []" in r.stdout
    assert f.read_text() == "A=2\nBOT_TOKEN=9:AA/b&c|d-_x\n# comment\nC=3\nNEW=v\n"


@pytest.mark.parametrize("fn,value,ok", [
    ("valid_token", "123456789:AAH" + "x" * 30, True), ("valid_token", "123456:replace_me", False),
    ("valid_token", "abc", False),
    ("valid_owners", "123", True), ("valid_owners", "1,22,333", True), ("valid_owners", "1,,2", False),
    ("valid_owners", "-5", False), ("valid_owners", "1 2", False),
    ("valid_domain", "bot.example.com", True), ("valid_domain", "a-b.example.io", True),
    ("valid_domain", "localhost", False), ("valid_domain", "bad_domain.com", False),
    ("valid_domain", "x.com; rm -rf /", False), ("valid_domain", "-a.com", False),
    ("valid_email", "me@example.com", True), ("valid_email", "me@", False), ("valid_email", "a b@c.de", False),
])
def test_validators(fn, value, ok):
    r = bash(f'{fn} "$V"', env={"V": value})
    assert (r.returncode == 0) is ok, f"{fn}({value!r})"


def test_parse_sender_id_ignores_groups():
    updates = ('{"ok":true,"result":['
               '{"update_id":1,"message":{"message_id":1,"from":{"id":555,"is_bot":false},'
               '"chat":{"id":-100,"title":"g","type":"supergroup"},"text":"hi"}},'
               '{"update_id":2,"message":{"message_id":2,"from":{"id":777,"is_bot":false,"first_name":"A"},'
               '"chat":{"id":777,"first_name":"A","type":"private"},"text":"/start"}}]}')
    r = bash('parse_sender_id <<<"$J"', env={"J": updates})
    assert r.stdout.strip() == "777"
    assert bash('parse_sender_id <<<"$J"', env={"J": '{"ok":true,"result":[]}'}).stdout.strip() == ""


def test_nginx_conf_generation(tmp_path):
    out = tmp_path / "crymoder.conf"
    r = bash(f'''sys() {{ "$@"; }}
nginx_paths() {{ NGINX_CONF="{out}"; }}
nginx_write_conf bot.example.com 8000''')
    assert r.returncode == 0, r.stderr
    conf = out.read_text()
    assert "server_name bot.example.com;" in conf and "proxy_pass http://127.0.0.1:8000;" in conf
    assert "proxy_set_header Host $host;" in conf and "__DOMAIN__" not in conf and "__PORT__" not in conf


# ---------- обновление: настоящий git, подставные docker/systemd ----------
def git(*a, cwd):
    subprocess.run(["git", *a], cwd=cwd, check=True, capture_output=True,
                   env={**os.environ, "GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@t",
                        "GIT_COMMITTER_NAME": "t", "GIT_COMMITTER_EMAIL": "t@t"})


def commit_version(repo, version):
    (repo / "crymoder").mkdir(exist_ok=True)
    (repo / "crymoder" / "__main__.py").write_text("")
    (repo / "crymoder" / "__init__.py").write_text(f'__version__ = "{version}"\n')
    git("add", "-A", cwd=repo)
    git("commit", "-qm", f"v{version}", cwd=repo)


@pytest.fixture
def installed(tmp_path):
    origin = tmp_path / "origin"
    origin.mkdir()
    git("init", "-q", "-b", "main", cwd=origin)
    commit_version(origin, "0.1.0")
    inst = tmp_path / "inst"
    git("clone", "-q", "--depth", "1", f"file://{origin}", str(inst), cwd=tmp_path)
    (inst / ".install.conf").write_text("MODE=venv\nBRANCH=main\n")
    return origin, inst


def head(repo):
    return subprocess.run(["git", "rev-parse", "HEAD"], cwd=repo, capture_output=True, text=True).stdout.strip()


STUBS = '''setup_privileges() { :; }; ensure_base() { :; }
db_backup() { echo /tmp/backup.db; }
svc_apply() { return 0; }
wait_healthy() { return 0; }'''


def test_update_applies_new_version_and_is_idempotent(installed):
    origin, inst = installed
    old = head(inst)
    r = bash(f"{STUBS}\nAUTO=1 cmd_update", env={"INSTALL_DIR": str(inst)})
    assert r.returncode == 0 and "Уже установлена последняя версия" in r.stdout, r.stdout + r.stderr
    commit_version(origin, "0.2.0")
    r = bash(f"{STUBS}\nAUTO=1 cmd_update", env={"INSTALL_DIR": str(inst)})
    assert r.returncode == 0, r.stdout + r.stderr
    assert "0.1.0" in r.stdout and "0.2.0" in r.stdout and "Бэкап базы" in r.stdout
    assert head(inst) == head(origin) != old
    assert 'version = "0.2.0"' in (inst / "crymoder" / "__init__.py").read_text().replace("__", "")


def test_update_rolls_back_when_new_version_does_not_start(installed, tmp_path):
    origin, inst = installed
    old = head(inst)
    commit_version(origin, "0.2.0")
    flag = tmp_path / "calls"
    # первая проверка здоровья (новая версия) — провал, вторая (после отката) — успех
    code = f'''{STUBS}
wait_healthy() {{ if [ -f "{flag}" ]; then return 0; else touch "{flag}"; return 1; fi; }}
AUTO=1 cmd_update'''
    r = bash(code, env={"INSTALL_DIR": str(inst)})
    assert r.returncode == 1, r.stdout + r.stderr
    assert "откатываюсь" in r.stderr and "Откат выполнен" in r.stderr
    assert head(inst) == old, "после неудачного обновления код возвращён на прежнюю версию"


def test_update_aborts_without_backup_and_changes_nothing(installed):
    origin, inst = installed
    old = head(inst)
    commit_version(origin, "0.2.0")
    r = bash(f"{STUBS}\ndb_backup() {{ return 1; }}\nAUTO=1 cmd_update", env={"INSTALL_DIR": str(inst)})
    assert r.returncode != 0 and "бэкап" in r.stderr.lower()
    assert head(inst) == old
    r = bash(f"{STUBS}\ndb_backup() {{ return 1; }}\nFORCE=1 AUTO=1 cmd_update", env={"INSTALL_DIR": str(inst)})
    assert r.returncode == 0 and head(inst) == head(origin), "--force обновляет и без бэкапа"


def test_update_stashes_local_edits(installed):
    origin, inst = installed
    commit_version(origin, "0.2.0")
    (inst / "crymoder" / "__main__.py").write_text("# my local edit\n")
    r = bash(f"{STUBS}\nAUTO=1 cmd_update", env={"INSTALL_DIR": str(inst)})
    assert r.returncode == 0 and "отложены" in r.stderr, r.stdout + r.stderr
    assert head(inst) == head(origin)
    stash = subprocess.run(["git", "stash", "list"], cwd=inst, capture_output=True, text=True).stdout
    assert "crymoder-update" in stash


def test_env_install_dir_is_not_overwritten(installed):
    """Команда `crymoder` передаёт INSTALL_DIR через окружение — скрипт не должен его затирать."""
    _, inst = installed
    r = bash("load_install && echo \"$INSTALL_DIR|$MODE\"", env={"INSTALL_DIR": str(inst)})
    assert r.stdout.strip() == f"{inst}|venv", r.stderr
