import gzip
import io
import tarfile
import zipfile

import pytest

from agentl.connectors.files import archive_read, csv_read, file_read, resolve_encoding, txt_read


def _zip(path, members):
    with zipfile.ZipFile(path, "w") as z:
        for name, data in members.items():
            z.writestr(name, data)
    return path


def test_zip_members_by_pattern_then_csv(tmp_path):
    z = _zip(
        tmp_path / "data.zip",
        {"docs/README.txt": "x", "2024/REPORT1.CSV": "a,b\n1,2\n", "2024/report2.csv": "a,b\n3,4\n"},
    )
    members = archive_read(path=str(z), pattern=r"(?i)report\d+\.csv$")
    assert [m["member"] for m in members] == ["2024/REPORT1.CSV", "2024/report2.csv"]
    assert members[0]["size"] == len("a,b\n1,2\n")
    rows = csv_read(members)
    assert [r["a"] for r in rows] == ["1", "3"]


def test_no_match_lists_members(tmp_path):
    z = _zip(tmp_path / "d.zip", {"one.csv": "a\n1\n", "two.txt": "t"})
    with pytest.raises(ValueError, match=r"one\.csv, two\.txt"):
        archive_read(path=str(z), pattern="nothing")


def test_tar_and_gz(tmp_path):
    t = tmp_path / "d.tar.gz"
    with tarfile.open(t, "w:gz") as tf:
        data = b"a\n1\n"
        info = tarfile.TarInfo("in/rows.csv")
        info.size = len(data)
        tf.addfile(info, io.BytesIO(data))
        link = tarfile.TarInfo("in/link.csv")
        link.type = tarfile.SYMTYPE
        link.linkname = "/etc/passwd"
        tf.addfile(link)
    assert [m["member"] for m in archive_read(path=str(t), pattern=r"\.csv$")] == ["in/rows.csv"]
    g = tmp_path / "rows.csv.gz"
    g.write_bytes(gzip.compress(b"a\n7\n"))
    (m,) = archive_read(path=str(g))
    assert m["member"] == "rows.csv"
    assert csv_read([m]) == [{"a": "7"}]


def test_traversal_rejected(tmp_path):
    z = _zip(tmp_path / "evil.zip", {"../../escape.csv": "a\n1\n"})
    with pytest.raises(ValueError, match="unsafe path"):
        archive_read(path=str(z))
    t = tmp_path / "evil.tar"
    with tarfile.open(t, "w") as tf:
        info = tarfile.TarInfo("../escape.csv")
        info.size = 1
        tf.addfile(info, io.BytesIO(b"x"))
    with pytest.raises(ValueError, match="unsafe path"):
        archive_read(path=str(t))


def test_bomb_limits(tmp_path):
    z = tmp_path / "bomb.zip"
    with zipfile.ZipFile(z, "w", zipfile.ZIP_DEFLATED) as zf:
        zf.writestr("big.txt", "0" * 100_000)
    with pytest.raises(ValueError, match="max_extract_bytes"):
        archive_read(path=str(z), max_extract_bytes=10_000)
    many = _zip(tmp_path / "many.zip", {f"f{i}.txt": "x" for i in range(5)})
    with pytest.raises(ValueError, match="max_members"):
        archive_read(path=str(many), max_members=4)


def test_not_an_archive(tmp_path):
    f = tmp_path / "x.zip"
    f.write_text("plain")
    with pytest.raises(ValueError, match="not a zip"):
        archive_read(path=str(f))


def test_encoding_fallback_recorded(tmp_path):
    latin = tmp_path / "latin.csv"
    latin.write_bytes("name\nJosé\n".encode("latin-1"))
    utf = tmp_path / "utf.csv"
    utf.write_text("name\nJosé\n", encoding="utf-8")
    assert resolve_encoding(latin, "auto") == "latin-1"
    assert resolve_encoding(utf, ["utf-8", "latin-1"]) == "utf-8"
    assert csv_read(path=str(latin), encoding=["utf-8", "latin-1"]) == [
        {"name": "José", "_encoding": "latin-1"}
    ]
    assert csv_read(path=str(utf), encoding="auto") == [{"name": "José", "_encoding": "utf-8"}]
    assert csv_read(path=str(utf)) == [{"name": "José"}]  # a plain name adds no metadata
    assert txt_read(path=str(latin), encoding="auto")[0]["_encoding"] == "latin-1"
    assert file_read(path=str(latin), encoding="auto")[0]["_encoding"] == "latin-1"
    with pytest.raises(ValueError, match="none of the encodings"):
        resolve_encoding(latin, ["utf-8", "ascii"])
