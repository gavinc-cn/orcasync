import sys
import argparse

from orcasync.cli import main


class TestCLI:
    def test_server_defaults(self, monkeypatch):
        monkeypatch.setattr(sys, "argv", ["orcasync", "server"])
        monkeypatch.setattr(
            "orcasync.cli.run_server", lambda h, p: (_ for _ in ()).throw(SystemExit(0))
        )
        with pytest.raises(SystemExit):
            main()

    def test_server_custom_host_port(self, monkeypatch):
        captured = {}

        def fake_run_server(host, port):
            captured["host"] = host
            captured["port"] = port
            raise SystemExit(0)

        monkeypatch.setattr(sys, "argv", ["orcasync", "server", "--host", "1.2.3.4", "--port", "9999"])
        monkeypatch.setattr("orcasync.cli.run_server", fake_run_server)
        with pytest.raises(SystemExit):
            main()
        assert captured["host"] == "1.2.3.4"
        assert captured["port"] == 9999

    def test_client_required_args(self, monkeypatch):
        monkeypatch.setattr(sys, "argv", ["orcasync", "client", "--local", "/tmp/a", "--remote", "/tmp/b"])
        monkeypatch.setattr(
            "orcasync.cli.run_client", lambda l, r, h, p: (_ for _ in ()).throw(SystemExit(0))
        )
        with pytest.raises(SystemExit):
            main()

    def test_client_custom_host_port(self, monkeypatch):
        captured = {}

        def fake_run_client(local, remote, host, port):
            captured.update(local=local, remote=remote, host=host, port=port)
            raise SystemExit(0)

        monkeypatch.setattr(
            sys,
            "argv",
            ["orcasync", "client", "-l", "/tmp/x", "-r", "/tmp/y", "-H", "10.0.0.1", "-p", "7777"],
        )
        monkeypatch.setattr("orcasync.cli.run_client", fake_run_client)
        with pytest.raises(SystemExit):
            main()
        assert captured["host"] == "10.0.0.1"
        assert captured["port"] == 7777

    def test_no_subcommand_fails(self, monkeypatch):
        monkeypatch.setattr(sys, "argv", ["orcasync"])
        with pytest.raises(SystemExit):
            main()

    def test_client_missing_local_fails(self, monkeypatch):
        monkeypatch.setattr(sys, "argv", ["orcasync", "client", "--remote", "/tmp/b"])
        with pytest.raises(SystemExit):
            main()


import pytest
