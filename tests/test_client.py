"""Pruebas del backend contra un repositorio local temporal: pytest tests/"""
import os
import shutil
import subprocess

import pytest

from rabbitsvn.svn.client import SvnClient, SvnError

pytestmark = pytest.mark.skipif(not shutil.which("svnadmin"), reason="svnadmin no instalado")


@pytest.fixture()
def wc(tmp_path):
    os.environ.setdefault("LC_CTYPE", "C.UTF-8")
    repo = tmp_path / "repo"
    subprocess.run(["svnadmin", "create", str(repo)], check=True)
    url = f"file://{repo}"
    c = SvnClient()
    c.execute(c.cmd_mkdir([url + "/trunk", url + "/branches"], "init"))
    path = tmp_path / "wc"
    c.execute(c.cmd_checkout(url + "/trunk", str(path)))
    return c, url, path


def test_ciclo_completo(wc):
    c, url, path = wc
    f = path / "año.txt"
    f.write_text("uno\n")
    c.execute(c.cmd_add([str(f)]))
    c.execute(c.cmd_commit([str(f)], "primer commit"))
    f.write_text("uno\ndos\n")
    st = {os.path.basename(e.path): e for e in c.status([str(path)])}
    assert st["año.txt"].item == "modified"
    assert "+dos" in c.diff([str(f)])
    logs = c.log(str(path), revision="HEAD:1")
    assert logs[0].message == "primer commit"
    assert [b.text for b in c.blame(str(f))] == ["uno", "dos"]
    c.execute(c.cmd_revert([str(f)]))
    assert c.status([str(path)]) == []


def test_ramas_y_propiedades(wc):
    c, url, path = wc
    c.execute(c.cmd_copy(url + "/trunk", url + "/branches/b1", "rama"))
    assert any(e.name == "b1" for e in c.list(url + "/branches"))
    c.execute(c.cmd_update([str(path)]))
    c.execute(c.cmd_switch(url + "/branches/b1", str(path)))
    assert c.info(str(path))[0].url.endswith("/branches/b1")
    c.propset("svn:ignore", "*.log\n", str(path))
    assert c.propget("svn:ignore", str(path)) == "*.log\n"


def test_error(wc):
    c, url, _ = wc
    with pytest.raises(SvnError):
        c.info(url + "/no-existe")


def test_merge_rangos():
    argv = SvnClient().cmd_merge("/wc", "^/trunk", ["5-8", "-3", "10"]).argv
    assert ["-r", "4:8"] == argv[argv.index("-r"):argv.index("-r") + 2]
    assert "-3" in argv and "10" in argv
