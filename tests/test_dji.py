import json
import os
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import dji  # noqa: E402


def mp4_with_model(model=b"DJI OsmoAction4"):
    ftyp = (16).to_bytes(4, "big") + b"ftypisom" + b"\0" * 4
    mdat_body = b"\0" * 64
    mdat = (8 + len(mdat_body)).to_bytes(4, "big") + b"mdat" + mdat_body
    udta = b"\0\0\0\x08hdlrDJI meta" + model
    moov = (8 + len(udta)).to_bytes(4, "big") + b"moov" + udta
    return ftyp + mdat + moov


class Fixture:
    """A fake card, sysfs, and home directory under one temp dir."""

    def __init__(self, usb_product="OsmoAction4"):
        self.tmp = tempfile.TemporaryDirectory()
        root = Path(self.tmp.name)
        self.home = root / "home"
        self.home.mkdir()
        self.mount = root / "media/SD Card"
        self.folder = self.mount / "DCIM/DJI_001"
        self.folder.mkdir(parents=True)
        self.sys = root / "sys"
        (self.sys / "bus/usb/devices").mkdir(parents=True)
        (self.sys / "class/block").mkdir(parents=True)
        usb = self.sys / "devices/pci0/usb5/5-1"
        disk = usb / "5-1:1.0/host0/block/sda"
        disk.mkdir(parents=True)
        if usb_product:
            (usb / "idVendor").write_text("2ca3\n")
            (usb / "product").write_text(usb_product + "\n")
            (self.sys / "bus/usb/devices/5-1").symlink_to(usb)
        else:
            (usb / "idVendor").write_text("0bda\n")
        (self.sys / "class/block/sda").symlink_to(disk)
        self.mounts = f"/dev/sda {str(self.mount).replace(' ', chr(92) + '040')} exfat rw 0 0\nproc /proc proc rw 0 0\n"

    def add(self, name, data=b"x" * 100):
        path = self.folder / name
        path.write_bytes(data)
        return path

    def kw(self):
        return {"mounts_text": self.mounts, "sysroot": str(self.sys), "home": str(self.home)}

    def close(self):
        self.tmp.cleanup()


class DetectTests(unittest.TestCase):
    def setUp(self):
        self.fx = Fixture()

    def tearDown(self):
        self.fx.close()

    def test_short_model(self):
        self.assertEqual(dji.short_model("OsmoAction4"), "Action4")
        self.assertEqual(dji.short_model("DJI OsmoPocket3"), "Pocket3")
        self.assertEqual(dji.short_model("Osmo360"), "Osmo360")
        self.assertEqual(dji.short_model("../evil"), "evil")
        self.assertEqual(dji.short_model(""), "DJI")
        self.assertEqual(dji.short_model(".."), "DJI")

    def test_expand_dest(self):
        self.assertEqual(dji.expand_dest("~/Videos/{model}", "Action4", "/h"), Path("/h/Videos/Action4"))
        self.assertEqual(dji.expand_dest("", "Action4", "/h"), Path("/h/Videos/Action4"))
        self.assertEqual(dji.expand_dest("/mnt/nas/{model}/raw", "Action4", "/h"), Path("/mnt/nas/Action4/raw"))
        with self.assertRaises(ValueError):
            dji.expand_dest("Videos/{model}", "Action4", "/h")

    def test_parse_mounts_unescapes_spaces(self):
        mounts = dji.parse_mounts(self.fx.mounts)
        self.assertEqual(mounts[0], ("/dev/sda", str(self.fx.mount), "exfat"))

    def test_status_over_usb(self):
        self.fx.add("DJI_20261115115741_0001_D.MP4")
        self.fx.add("DJI_20261115115741_0001_D.LRF")
        self.fx.add("DJI_20261116090000_0002_D.JPG")
        self.fx.add("notes.txt")
        state = dji.status(**self.fx.kw())
        self.assertIsNone(state["error"])
        self.assertEqual(state["unmounted"], [])
        card = state["cards"][0]
        self.assertEqual(card["model"], "OsmoAction4")
        self.assertEqual(card["connection"], "usb")
        self.assertEqual(card["dest"], str(self.fx.home / "Videos/Action4"))
        self.assertEqual(card["counts"]["video"], {"total": 1, "new": 1})
        self.assertEqual(card["counts"]["photo"], {"total": 1, "new": 1})
        self.assertEqual(card["counts"]["proxy"], {"total": 0, "new": 0})
        self.assertEqual(card["newFiles"], 2)
        json.dumps(state)

    def test_proxies_and_no_photos(self):
        self.fx.add("DJI_20261115115741_0001_D.MP4")
        self.fx.add("DJI_20261115115741_0001_D.LRF")
        self.fx.add("DJI_20261116090000_0002_D.JPG")
        card = dji.status(photos=False, proxies=True, **self.fx.kw())["cards"][0]
        self.assertEqual(card["counts"]["photo"]["total"], 0)
        self.assertEqual(card["counts"]["proxy"]["total"], 1)

    def test_bad_dest_reports_error(self):
        self.fx.add("DJI_20261115115741_0001_D.MP4")
        state = dji.status(dest_template="relative/{model}", **self.fx.kw())
        self.assertIn("absolute", state["error"])
        self.assertEqual(state["cards"][0]["dest"], "")

    def test_unmounted_camera(self):
        state = dji.status(mounts_text="proc /proc proc rw 0 0\n", sysroot=str(self.fx.sys), home=str(self.fx.home))
        self.assertEqual(state["cards"], [])
        self.assertEqual(state["unmounted"], ["OsmoAction4"])

    def test_non_card_filesystems_ignored(self):
        self.fx.add("DJI_20261115115741_0001_D.MP4")
        mounts = self.fx.mounts.replace(" exfat ", " ext4 ")
        state = dji.status(mounts_text=mounts, sysroot=str(self.fx.sys), home=str(self.fx.home))
        self.assertEqual(state["cards"], [])


class CardReaderTests(unittest.TestCase):
    def setUp(self):
        self.fx = Fixture(usb_product=None)

    def tearDown(self):
        self.fx.close()

    def test_model_sniffed_from_mp4(self):
        self.fx.add("DJI_20261115115741_0001_D.MP4", mp4_with_model(b"DJI OsmoAction5Pro"))
        card = dji.status(**self.fx.kw())["cards"][0]
        self.assertEqual(card["model"], "OsmoAction5Pro")
        self.assertEqual(card["folder"], "Action5Pro")
        self.assertEqual(card["connection"], "card")

    def test_model_sniffed_from_jpg(self):
        self.fx.add("DJI_20261115115741_0001_D.JPG", b"\xff\xd8 exif DJI OsmoPocket3 \x00")
        self.assertEqual(dji.status(**self.fx.kw())["cards"][0]["folder"], "Pocket3")

    def test_unknown_model_falls_back(self):
        self.fx.add("DJI_20261115115741_0001_D.MP4", b"not an mp4")
        self.assertEqual(dji.status(**self.fx.kw())["cards"][0]["folder"], "DJI")


class ImportTests(unittest.TestCase):
    def setUp(self):
        self.fx = Fixture()
        self.events = []

    def tearDown(self):
        self.fx.close()

    def run_import(self, **kw):
        return dji.run_import(emit=self.events.append, **self.fx.kw(), **kw)

    def dest(self, day, name):
        return self.fx.home / "Videos/Action4" / day / name

    def done(self):
        return [e for e in self.events if e["event"] == "done"][-1]

    def test_copies_into_dated_folders(self):
        v = self.fx.add("DJI_20261115115741_0001_D.MP4", b"video" * 1000)
        self.fx.add("DJI_20261116090000_0002_D.JPG", b"photo")
        os.utime(v, (1_700_000_000, 1_700_000_000))
        self.assertEqual(self.run_import(), 0)
        out = self.dest("2026-11-15", v.name)
        self.assertEqual(out.read_bytes(), b"video" * 1000)
        self.assertEqual(int(out.stat().st_mtime), 1_700_000_000)
        self.assertTrue(self.dest("2026-11-16", "DJI_20261116090000_0002_D.JPG").exists())
        self.assertTrue(v.exists(), "copy mode must leave the card alone")
        self.assertEqual(self.done()["copied"], 2)
        self.assertFalse(list(out.parent.glob(".*.part")))

    def test_second_run_skips(self):
        self.fx.add("DJI_20261115115741_0001_D.MP4")
        self.run_import()
        self.events.clear()
        self.assertEqual(self.run_import(), 0)
        self.assertEqual(self.done()["copied"], 0)
        self.assertEqual(self.done()["skipped"], 1)

    def test_move_deletes_after_verify(self):
        v = self.fx.add("DJI_20261115115741_0001_D.MP4")
        self.assertEqual(self.run_import(move=True), 0)
        self.assertFalse(v.exists())
        self.assertTrue(self.dest("2026-11-15", v.name).exists())
        self.assertEqual(self.done()["deleted"], 1)

    def test_move_of_already_imported_file_verifies_first(self):
        v = self.fx.add("DJI_20261115115741_0001_D.MP4", b"a" * 100)
        target = self.dest("2026-11-15", v.name)
        target.parent.mkdir(parents=True)
        target.write_bytes(b"b" * 100)  # same size, different content
        self.assertEqual(self.run_import(move=True), 1)
        self.assertTrue(v.exists(), "mismatched copy must not delete the original")
        self.assertTrue(self.done()["errors"])

    def test_conflict_is_reported_not_overwritten(self):
        v = self.fx.add("DJI_20261115115741_0001_D.MP4", b"a" * 100)
        target = self.dest("2026-11-15", v.name)
        target.parent.mkdir(parents=True)
        target.write_bytes(b"other")
        self.assertEqual(self.run_import(), 1)
        self.assertEqual(target.read_bytes(), b"other")
        self.assertIn("already exists", self.done()["errors"][0])

    def test_dry_run_changes_nothing(self):
        self.fx.add("DJI_20261115115741_0001_D.MP4")
        self.assertEqual(self.run_import(dry_run=True, move=True), 0)
        self.assertFalse((self.fx.home / "Videos").exists())
        self.assertEqual(len([e for e in self.events if e["event"] == "file"]), 1)

    def test_no_card(self):
        self.assertEqual(dji.run_import(emit=self.events.append, mounts_text="", sysroot=str(self.fx.sys)), 1)
        self.assertEqual(self.events[0]["event"], "error")

    def test_wrong_card_mount(self):
        self.fx.add("DJI_20261115115741_0001_D.MP4")
        self.assertEqual(self.run_import(card_mount="/nowhere"), 1)

    def test_not_enough_space(self):
        self.fx.add("DJI_20261115115741_0001_D.MP4")
        original = dji.free_space
        dji.free_space = lambda path: 1
        try:
            self.assertEqual(self.run_import(), 1)
        finally:
            dji.free_space = original
        self.assertIn("free space", self.events[-1]["message"])

    def test_failed_copy_leaves_no_part_file(self):
        v = self.fx.add("DJI_20261115115741_0001_D.MP4")

        def boom(n):
            raise OSError("disk unplugged")

        with self.assertRaises(OSError):
            dji.copy_file(v, self.dest("2026-11-15", v.name), boom, verify=False)
        self.assertEqual(list(self.dest("2026-11-15", "x").parent.iterdir()), [])

    def test_cli_status_json_is_one_line(self):
        import contextlib
        import io

        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            self.assertEqual(dji.main(["status", "--json"]), 0)
        lines = buf.getvalue().splitlines()
        self.assertEqual(len(lines), 1)
        self.assertIn("cards", json.loads(lines[0]))


class ProgressTests(unittest.TestCase):
    def test_render_bar_fits_width(self):
        line = dji.render_bar(28_400_000_000, 45_700_000_000, 41_200_000, 80, "3/11 ")
        self.assertLessEqual(len(line), 80)
        self.assertTrue(line.startswith("3/11 ━"))
        self.assertIn(" 62%", line)
        self.assertIn("41.2 MB/s", line)
        self.assertIn("6:59 left", line)

    def test_render_bar_edges(self):
        self.assertNotIn("─", dji.render_bar(10, 10, 0, 60))
        self.assertNotIn("━", dji.render_bar(0, 10, 0, 60))
        self.assertNotIn("left", dji.render_bar(5, 10, 0, 60))
        self.assertLessEqual(len(dji.render_bar(5, 10, 1e6, 20, "11/11 ")), 20)

    def test_human_duration(self):
        self.assertEqual(dji.human_duration(65), "1:05")
        self.assertEqual(dji.human_duration(3725), "1:02:05")

    def test_emitter_draws_and_finishes_bar(self):
        import io

        out = io.StringIO()
        now = [0.0]
        emit = dji.HumanEmitter(stream=out, tty=True, width=70, clock=lambda: now[0])
        emit({"event": "start", "model": "OsmoAction4", "dest": "/d", "files": 1, "bytes": 100,
              "move": False, "dryRun": False})
        emit({"event": "file", "index": 1, "files": 1, "name": "a.MP4", "size": 100, "day": "2026-11-15",
              "action": "copy"})
        now[0] = 2.0
        emit({"event": "progress", "bytes": 50, "total": 100})
        self.assertIn(" 50%", out.getvalue().rsplit("\r", 1)[-1])
        self.assertIn("25 B/s", out.getvalue())
        emit({"event": "progress", "bytes": 100, "total": 100})
        emit({"event": "done", "dest": "/d", "copied": 1, "skipped": 0, "deleted": 0, "errors": [],
              "dryRun": False})
        text = out.getvalue()
        self.assertIn("100%", text)
        self.assertTrue(text.endswith("Done: 1 copied, 0 already there, 0 removed from card\n"))

    def test_emitter_without_tty_prints_no_bar(self):
        import io

        out = io.StringIO()
        emit = dji.HumanEmitter(stream=out, tty=False)
        emit({"event": "start", "model": "M", "dest": "/d", "files": 1, "bytes": 100, "move": False,
              "dryRun": False})
        emit({"event": "progress", "bytes": 50, "total": 100})
        self.assertNotIn("━", out.getvalue())


class Mp4Tests(unittest.TestCase):
    def test_moov_after_mdat(self):
        with tempfile.NamedTemporaryFile(suffix=".MP4", delete=False) as f:
            f.write(mp4_with_model())
        try:
            self.assertIn(b"OsmoAction4", dji.mp4_moov(f.name))
            self.assertEqual(dji.sniff_model(f.name), "OsmoAction4")
        finally:
            os.unlink(f.name)

    def test_truncated_box(self):
        with tempfile.NamedTemporaryFile(suffix=".MP4", delete=False) as f:
            f.write(b"\0\0\0\x02junk")
        try:
            self.assertEqual(dji.mp4_moov(f.name), b"")
        finally:
            os.unlink(f.name)


if __name__ == "__main__":
    unittest.main(verbosity=1)
