from importlib.metadata import version

from codecortex import __version__
from codecortex.interfaces.cli.main import main


def test_package_version_and_cli(capsys):
    assert __version__ == version("codecortex")
    assert main(["--version"]) == 0
    assert capsys.readouterr().out.strip() == __version__
