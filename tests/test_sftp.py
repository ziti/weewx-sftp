"""Unit tests for bin/user/sftp.py."""

import logging
import os

import pytest

from user.sftp import VERSION, SFTPGenerator, SFTPUploader


def make_uploader(**overrides):
    kwargs = dict(
        server="host.example",
        user="bob",
        password="secret",
        local_root="/srv/reports",
        remote_root="/remote/pub",
        port=22,
        name="SFTP",
        max_tries=3,
    )
    kwargs.update(overrides)
    return SFTPUploader(**kwargs)


# --------------------------------------------------------------------------
# construction / small helpers
# --------------------------------------------------------------------------

def test_version_is_nonempty_string():
    assert isinstance(VERSION, str) and VERSION


def test_init_normalizes_roots():
    up = make_uploader(local_root="a/b/../c/", remote_root="/remote/./pub/")
    assert up.local_root == os.path.normpath("a/b/../c/")
    assert up.remote_root == os.path.normpath("/remote/./pub/")


@pytest.mark.parametrize(
    "rel_dir, skipped",
    [
        ("./.git/objects", True),
        ("./CVS", True),
        ("./.svn/text-base", True),
        ("./new-belchertown", False),
        (".", False),
    ],
)
def test_skip_dir(rel_dir, skipped):
    assert make_uploader()._skip_dir(rel_dir) is skipped


@pytest.mark.parametrize("name", ["#SFTP.last", ".#index.html", "index.html~"])
def test_skip_file_temporary_names(tmp_path, name):
    p = tmp_path / name
    p.write_text("x")
    assert make_uploader()._skip_file(0, {str(p)}, str(p)) is True


def test_skip_file_new_file_is_uploaded(tmp_path):
    p = tmp_path / "index.html"
    p.write_text("x")
    # not in the fileset -> must not be skipped
    assert make_uploader()._skip_file(time_far_future(), set(), str(p)) is False


def test_skip_file_unchanged_is_skipped(tmp_path):
    p = tmp_path / "index.html"
    p.write_text("x")
    os.utime(p, (1_000, 1_000))
    assert make_uploader()._skip_file(2_000, {str(p)}, str(p)) is True


def test_skip_file_modified_is_uploaded(tmp_path):
    p = tmp_path / "index.html"
    p.write_text("x")
    os.utime(p, (5_000, 5_000))
    assert make_uploader()._skip_file(2_000, {str(p)}, str(p)) is False


def time_far_future():
    return 10_000_000_000


# --------------------------------------------------------------------------
# last-upload state file
# --------------------------------------------------------------------------

def test_last_upload_roundtrip(tmp_path):
    up = make_uploader(local_root=str(tmp_path))
    files = {str(tmp_path / "a"), str(tmp_path / "b")}
    up.save_last_upload(1234.5, files)
    ts, got = up.get_last_upload()
    assert ts == 1234.5
    assert got == files


def test_get_last_upload_missing_file(tmp_path):
    ts, files = make_uploader(local_root=str(tmp_path)).get_last_upload()
    assert ts == 0
    assert files == set()


def test_get_last_upload_corrupt_file_is_reset(tmp_path):
    tsfile = tmp_path / "#SFTP.last"
    tsfile.write_bytes(b"not a pickle")
    up = make_uploader(local_root=str(tmp_path))
    ts, files = up.get_last_upload()
    assert (ts, files) == (0, set())
    assert not tsfile.exists()  # unreadable state file is removed


# --------------------------------------------------------------------------
# run(): connection handling
# --------------------------------------------------------------------------

def test_run_connects_with_password_only(paramiko_stub, tmp_path):
    up = make_uploader(local_root=str(tmp_path))
    up.run()
    (client,) = paramiko_stub._state["clients"]
    (kwargs,) = client.connect_calls
    assert kwargs["hostname"] == "host.example"
    assert kwargs["username"] == "bob"
    assert kwargs["port"] == 22
    assert kwargs["password"] == "secret"
    assert "key_filename" not in kwargs and "passphrase" not in kwargs
    assert client.host_keys_loaded is True
    assert isinstance(client.policy, paramiko_stub.AutoAddPolicy)


def test_run_connects_with_private_key(paramiko_stub, tmp_path):
    up = make_uploader(
        local_root=str(tmp_path),
        password=None,
        private_key="/home/bob/.ssh/id_ed25519",
        private_key_pass="hunter2",
    )
    up.run()
    (client,) = paramiko_stub._state["clients"]
    (kwargs,) = client.connect_calls
    assert kwargs["key_filename"] == "/home/bob/.ssh/id_ed25519"
    assert kwargs["passphrase"] == "hunter2"
    assert "password" not in kwargs


def test_run_retries_then_succeeds(paramiko_stub, tmp_path):
    paramiko_stub._state["fail_first"] = 2
    up = make_uploader(local_root=str(tmp_path), max_tries=3)
    up.run()
    clients = paramiko_stub._state["clients"]
    assert len(clients) == 3
    assert clients[0].closed and clients[1].closed  # failed attempts cleaned up
    assert clients[2].closed  # final one closed in finally


def test_run_gives_up_after_max_tries(paramiko_stub, tmp_path, caplog):
    paramiko_stub._state["fail_first"] = 99
    up = make_uploader(local_root=str(tmp_path), max_tries=3)
    with caplog.at_level(logging.ERROR):
        assert up.run() == 0
    assert len(paramiko_stub._state["clients"]) == 3
    assert "failed 3 attempts" in caplog.text


# --------------------------------------------------------------------------
# run(): file transfer
# --------------------------------------------------------------------------

def test_run_uploads_only_changed_files(paramiko_stub, tmp_path):
    root = tmp_path / "reports"
    root.mkdir()
    fresh = root / "index.html"
    fresh.write_text("new")
    stale = root / "old.html"
    stale.write_text("old")
    os.utime(stale, (1_000, 1_000))

    up = make_uploader(local_root=str(root), remote_root="/pub", max_tries=2)
    up.save_last_upload(2_000, {str(stale)})  # stale already uploaded, unchanged

    n = up.run()

    sftp = paramiko_stub._state["clients"][-1].sftp
    uploaded = [os.path.basename(remote) for _local, remote in sftp.put_calls]
    mkdirs = [c.replace(os.sep, "/") for c in sftp.mkdir_calls]
    assert n == 1
    assert uploaded == ["index.html"]
    assert "/pub" in mkdirs  # remote dir created
    # state file now records both files
    _ts, files = up.get_last_upload()
    assert {os.path.basename(f) for f in files} == {"index.html", "old.html"}


def test_run_put_failure_is_logged(paramiko_stub, tmp_path, caplog):
    root = tmp_path / "reports"
    root.mkdir()
    (root / "index.html").write_text("x")
    up = make_uploader(local_root=str(root), remote_root="/pub", max_tries=2)

    class ExplodingSFTP:
        def __init__(self):
            self.closed = False

        def stat(self, path):
            raise OSError(2, "nope", path)

        def mkdir(self, path):
            pass

        def put(self, local, remote):
            raise OSError("disk full")

        def close(self):
            self.closed = True

    # swap the sftp object the fake client will hand back
    real_open = paramiko_stub.SSHClient.open_sftp

    def open_sftp(self):
        self.sftp = ExplodingSFTP()
        return self.sftp

    paramiko_stub.SSHClient.open_sftp = open_sftp
    try:
        with caplog.at_level(logging.ERROR):
            n = up.run()
    finally:
        paramiko_stub.SSHClient.open_sftp = real_open

    assert n == 0
    assert "failed to upload file" in caplog.text


def test_run_closes_client_and_sftp(paramiko_stub, tmp_path):
    up = make_uploader(local_root=str(tmp_path))
    up.run()
    client = paramiko_stub._state["clients"][-1]
    assert client.closed is True
    assert client.sftp.closed is True


def test_run_makes_nested_remote_dirs_once(paramiko_stub, tmp_path):
    root = tmp_path / "reports"
    (root / "sub").mkdir(parents=True)
    (root / "a.html").write_text("a")
    (root / "sub" / "b.html").write_text("b")

    up = make_uploader(local_root=str(root), remote_root="/pub", max_tries=2)
    up.run()

    sftp = paramiko_stub._state["clients"][-1].sftp
    mkdirs = [c.replace(os.sep, "/") for c in sftp.mkdir_calls]
    # each directory is created exactly once
    assert sorted(mkdirs) == sorted(set(mkdirs))
    assert any(c.endswith("/sub") for c in mkdirs)


# --------------------------------------------------------------------------
# SFTPGenerator
# --------------------------------------------------------------------------

def _generator(tmp_path, skin, **cfg):
    config_dict = {"WEEWX_ROOT": str(tmp_path)}
    config_dict.update(cfg)
    return SFTPGenerator(config_dict, skin, None, None, None)


def test_generator_missing_required_param_is_soft_failure(tmp_path, caplog):
    gen = _generator(tmp_path, {"user": "bob", "password": "p", "path": "/pub"})
    with caplog.at_level(logging.INFO):
        assert gen.run() is None  # no exception, no server -> KeyError branch
    assert "missing parameter" in caplog.text


def test_generator_happy_path_logs_transfer(tmp_path, monkeypatch, caplog):
    calls = {}

    class FakeUploader:
        def __init__(self, **kwargs):
            calls["kwargs"] = kwargs

        def run(self):
            calls["ran"] = True
            return 5

    monkeypatch.setattr("user.sftp.SFTPUploader", FakeUploader)
    skin = {
        "server": "h", "user": "u", "password": "p", "path": "/pub",
        "HTML_ROOT": "public_html", "log_success": "true",
    }
    gen = _generator(tmp_path, skin)
    with caplog.at_level(logging.INFO):
        assert gen.run() is None
    assert calls["ran"] is True
    assert calls["kwargs"]["local_root"] == os.path.join(str(tmp_path), "public_html")
    assert "transferred 5 files" in caplog.text


def test_generator_swallows_uploader_exception(tmp_path, monkeypatch, caplog):
    class BoomUploader:
        def __init__(self, **kwargs):
            pass

        def run(self):
            raise RuntimeError("network went away")

    monkeypatch.setattr("user.sftp.SFTPUploader", BoomUploader)
    skin = {"server": "h", "user": "u", "password": "p", "path": "/pub"}
    gen = _generator(tmp_path, skin)
    with caplog.at_level(logging.ERROR):
        assert gen.run() is None  # must NOT propagate
    assert "network went away" in caplog.text


def test_state_file_uses_report_name(tmp_path):
    up = make_uploader(local_root=str(tmp_path), name="SftpBelchertown")
    up.save_last_upload(1.0, {"a"})
    assert (tmp_path / "#SftpBelchertown.last").exists()
    assert up.get_last_upload() == (1.0, {"a"})
