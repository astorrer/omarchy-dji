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

    def test_folder_name(self):
        self.assertEqual(dji.folder_name("OsmoAction4"), "OsmoAction4")
        self.assertEqual(dji.folder_name("DJI OsmoPocket3"), "OsmoPocket3")
        self.assertEqual(dji.folder_name("Osmo360"), "Osmo360")
        self.assertEqual(dji.folder_name("../evil"), "evil")
        self.assertEqual(dji.folder_name(""), "DJI")
        self.assertEqual(dji.folder_name(".."), "DJI")

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
        self.assertEqual(card["dest"], str(self.fx.home / "Videos/OsmoAction4"))
        self.assertFalse(card["destExists"])
        self.assertIsNone(card["destError"])
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
        self.assertEqual(card["folder"], "OsmoAction5Pro")
        self.assertEqual(card["connection"], "card")

    def test_model_sniffed_from_jpg(self):
        self.fx.add("DJI_20261115115741_0001_D.JPG", b"\xff\xd8 exif DJI OsmoPocket3 \x00")
        self.assertEqual(dji.status(**self.fx.kw())["cards"][0]["folder"], "OsmoPocket3")

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
        return self.fx.home / "Videos/OsmoAction4" / day / name

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

    def test_name_clash_is_imported_under_a_new_name(self):
        v = self.fx.add("DJI_20261115115741_0001_D.MP4", b"a" * 100)
        target = self.dest("2026-11-15", v.name)
        target.parent.mkdir(parents=True)
        target.write_bytes(b"other camera")
        self.assertEqual(dji.status(**self.fx.kw())["cards"][0]["renamed"], 1)
        self.assertEqual(self.run_import(), 0)
        self.assertEqual(target.read_bytes(), b"other camera", "existing file untouched")
        self.assertEqual(self.dest("2026-11-15", "DJI_20261115115741_0001_D-2.MP4").read_bytes(), b"a" * 100)
        self.assertEqual(self.done()["renamed"], 1)
        self.assertEqual(self.done()["errors"], [])
        file_event = [e for e in self.events if e["event"] == "file"][0]
        self.assertEqual(file_event["target"], "DJI_20261115115741_0001_D-2.MP4")

    def test_renamed_copy_counts_as_imported_next_time(self):
        v = self.fx.add("DJI_20261115115741_0001_D.MP4", b"a" * 100)
        target = self.dest("2026-11-15", v.name)
        target.parent.mkdir(parents=True)
        target.write_bytes(b"other camera")
        self.run_import()
        self.events.clear()
        self.assertEqual(dji.status(**self.fx.kw())["cards"][0]["newFiles"], 0)
        self.assertEqual(self.run_import(), 0)
        self.assertEqual(self.done()["copied"], 0)
        self.assertFalse(self.dest("2026-11-15", "DJI_20261115115741_0001_D-3.MP4").exists())

    def test_next_free_suffix(self):
        v = self.fx.add("DJI_20261115115741_0001_D.MP4", b"a" * 100)
        day = self.dest("2026-11-15", "x").parent
        day.mkdir(parents=True)
        (day / v.name).write_bytes(b"one")
        (day / "DJI_20261115115741_0001_D-2.MP4").write_bytes(b"two")
        self.run_import()
        self.assertEqual((day / "DJI_20261115115741_0001_D-3.MP4").read_bytes(), b"a" * 100)

    def test_same_name_twice_on_one_card(self):
        self.fx.add("DJI_20261115115741_0001_D.MP4", b"first")
        other = self.fx.mount / "DCIM/DJI_002"
        other.mkdir()
        (other / "DJI_20261115115741_0001_D.MP4").write_bytes(b"second!")
        self.assertEqual(self.run_import(), 0)
        day = self.dest("2026-11-15", "x").parent
        self.assertEqual(sorted(f.read_bytes() for f in day.iterdir()), [b"first", b"second!"])

    def test_move_with_rename_clears_the_card(self):
        v = self.fx.add("DJI_20261115115741_0001_D.MP4", b"a" * 100)
        target = self.dest("2026-11-15", v.name)
        target.parent.mkdir(parents=True)
        target.write_bytes(b"other camera")
        self.assertEqual(self.run_import(move=True), 0)
        self.assertFalse(v.exists())
        self.assertEqual(target.read_bytes(), b"other camera")

    def test_never_overwrites_a_file_that_appears_mid_import(self):
        v = self.fx.add("DJI_20261115115741_0001_D.MP4", b"a" * 100)
        target = self.dest("2026-11-15", v.name)
        target.parent.mkdir(parents=True)
        part = target.parent / "part"
        part.write_bytes(b"a" * 100)
        target.write_bytes(b"appeared")
        with self.assertRaises(OSError):
            dji.place_without_clobber(part, target)
        self.assertEqual(target.read_bytes(), b"appeared")

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


class CleanTests(unittest.TestCase):
    """dji.py clean: delete only what is imported and verified; copy nothing."""

    def setUp(self):
        self.fx = Fixture()
        self.events = []
        self.day = self.fx.home / "Videos/OsmoAction4/2026-11-15"

    def tearDown(self):
        self.fx.close()

    def run_clean(self, **kw):
        return dji.run_import(emit=self.events.append, clean=True, **self.fx.kw(), **kw)

    def done(self):
        return [e for e in self.events if e["event"] == "done"][-1]

    def imported(self, name, data):
        self.day.mkdir(parents=True, exist_ok=True)
        (self.day / name).write_bytes(data)
        return self.fx.add(name, data)

    def test_removes_verified_keeps_new_copies_nothing(self):
        done_file = self.imported("DJI_20261115115741_0001_D.MP4", b"a" * 100)
        new_file = self.fx.add("DJI_20261115120524_0002_D.MP4", b"b" * 100)
        card = dji.status(**self.fx.kw())["cards"][0]
        self.assertEqual((card["importedFiles"], card["importedBytes"]), (1, 100))
        self.assertEqual(self.run_clean(), 0)
        self.assertFalse(done_file.exists())
        self.assertTrue(new_file.exists(), "files not yet imported must stay on the card")
        self.assertFalse((self.day / new_file.name).exists(), "clean never copies")
        self.assertEqual((self.done()["mode"], self.done()["deleted"], self.done()["kept"]), ("clean", 1, 1))
        progress = [e for e in self.events if e["event"] == "progress"][-1]
        self.assertEqual((progress["bytes"], progress["total"]), (100, 100))

    def test_removes_proxy_of_cleaned_clip(self):
        clip = self.imported("DJI_20261115115741_0001_D.MP4", b"a" * 100)
        lrf = self.fx.add("DJI_20261115115741_0001_D.LRF", b"proxy")
        self.assertEqual(dji.status(**self.fx.kw())["cards"][0]["cleanProxies"], 1)
        self.assertEqual(self.run_clean(), 0)
        self.assertFalse(clip.exists())
        self.assertFalse(lrf.exists())
        self.assertEqual(self.done()["proxiesDeleted"], 1)

    def test_keeps_proxy_of_clip_not_yet_imported(self):
        self.fx.add("DJI_20261115120524_0002_D.MP4", b"b" * 100)
        lrf = self.fx.add("DJI_20261115120524_0002_D.LRF", b"proxy")
        self.assertEqual(dji.status(**self.fx.kw())["cards"][0]["cleanProxies"], 0)
        self.run_clean()
        self.assertTrue(lrf.exists())

    def test_removes_orphan_proxy_when_clip_is_in_library(self):
        self.day.mkdir(parents=True)
        (self.day / "DJI_20261115115741_0001_D.MP4").write_bytes(b"a" * 100)
        lrf = self.fx.add("DJI_20261115115741_0001_D.LRF", b"proxy")  # clip already gone from card
        card = dji.status(**self.fx.kw())["cards"][0]
        self.assertEqual((card["importedFiles"], card["cleanProxies"]), (0, 1))
        self.assertEqual(self.run_clean(), 0)
        self.assertFalse(lrf.exists())

    def test_keeps_orphan_proxy_when_clip_is_not_in_library(self):
        lrf = self.fx.add("DJI_20261115115741_0001_D.LRF", b"proxy")
        self.run_clean()
        self.assertTrue(lrf.exists())

    def test_keeps_proxy_being_imported_as_media(self):
        self.imported("DJI_20261115115741_0001_D.MP4", b"a" * 100)
        lrf = self.fx.add("DJI_20261115115741_0001_D.LRF", b"proxy")  # proxies on, not imported yet
        self.assertEqual(self.run_clean(proxies=True), 0)
        self.assertTrue(lrf.exists())

    def test_keeps_proxy_when_its_clip_fails_verification(self):
        clip = self.fx.add("DJI_20261115115741_0001_D.MP4", b"a" * 100)
        self.day.mkdir(parents=True)
        (self.day / clip.name).write_bytes(b"b" * 100)
        lrf = self.fx.add("DJI_20261115115741_0001_D.LRF", b"proxy")
        self.run_clean()
        self.assertTrue(clip.exists())
        self.assertTrue(lrf.exists())

    def test_dry_run_counts_proxies_but_keeps_them(self):
        self.imported("DJI_20261115115741_0001_D.MP4", b"a" * 100)
        lrf = self.fx.add("DJI_20261115115741_0001_D.LRF", b"proxy")
        self.run_clean(dry_run=True)
        self.assertTrue(lrf.exists())
        self.assertEqual(self.done()["proxiesDeleted"], 1)

    def test_import_move_removes_proxy_too(self):
        clip = self.fx.add("DJI_20261115115741_0001_D.MP4", b"a" * 100)
        lrf = self.fx.add("DJI_20261115115741_0001_D.LRF", b"proxy")
        self.assertEqual(dji.run_import(emit=self.events.append, move=True, **self.fx.kw()), 0)
        self.assertFalse(clip.exists())
        self.assertFalse(lrf.exists())
        self.assertTrue((self.day / clip.name).exists())

    def test_mismatched_copy_keeps_card_file(self):
        v = self.fx.add("DJI_20261115115741_0001_D.MP4", b"a" * 100)
        self.day.mkdir(parents=True)
        (self.day / v.name).write_bytes(b"b" * 100)  # same name and size, different bytes
        self.assertEqual(self.run_clean(), 1)
        self.assertTrue(v.exists())
        self.assertEqual(self.done()["deleted"], 0)
        self.assertTrue(self.done()["errors"])

    def test_dry_run_deletes_nothing(self):
        v = self.imported("DJI_20261115115741_0001_D.MP4", b"a" * 100)
        self.assertEqual(self.run_clean(dry_run=True), 0)
        self.assertTrue(v.exists())

    def test_cli_refuses_without_yes_when_not_interactive(self):
        import contextlib
        import io

        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            self.assertEqual(dji.main(["clean", "--json"]), 2)
        self.assertIn("--yes", buf.getvalue())

    def test_confirm_requires_typing_yes(self):
        import argparse
        import contextlib
        import io

        self.imported("DJI_20261115115741_0001_D.MP4", b"a" * 100)
        original = dji.status
        dji.status = lambda *a: original(*a, **self.fx.kw())
        try:
            args = argparse.Namespace(dest=dji.DEFAULT_DEST, photos=True, proxies=False, card=None)
            with contextlib.redirect_stdout(io.StringIO()) as out:
                self.assertFalse(dji.confirm_clean(args, ask=lambda prompt: "y"))
                self.assertTrue(dji.confirm_clean(args, ask=lambda prompt: "yes"))
            self.assertIn("Delete 1 imported file(s)", out.getvalue())
        finally:
            dji.status = original


class EjectTests(unittest.TestCase):
    def setUp(self):
        self.fx = Fixture()
        self.fx.add("DJI_20261115115741_0001_D.MP4")
        self.calls = []
        self.envs = []
        self.events = []
        self.synced = []

    def tearDown(self):
        self.fx.close()

    def fake_run(self, results):
        import subprocess

        def run(cmd, **kw):
            self.calls.append(cmd)
            self.envs.append(kw.get("env"))
            code, err = results[len(self.calls) - 1]
            return subprocess.CompletedProcess(cmd, code, "", err)
        return run

    def eject(self, results):
        return dji.eject(emit=self.events.append, mounts_text=self.fx.mounts, sysroot=str(self.fx.sys),
                         run=self.fake_run(results), sync=lambda: self.synced.append(True))

    def test_syncs_unmounts_then_powers_off(self):
        self.assertEqual(self.eject([(0, ""), (0, "")]), 0)
        self.assertEqual(self.synced, [True])
        self.assertEqual(self.calls[0], ["/usr/bin/udisksctl", "unmount", "-b", "/dev/sda", "--no-user-interaction"])
        self.assertEqual(self.calls[1], ["/usr/bin/udisksctl", "power-off", "-b", "/dev/sda", "--no-user-interaction"])
        self.assertEqual(self.events[-1]["event"], "ejected")
        self.assertTrue(self.events[-1]["poweredOff"])

    def test_busy_card_is_not_powered_off(self):
        err = "Error unmounting /dev/sda: GDBus.Error:org.freedesktop.UDisks2.Error.DeviceBusy: target is busy"
        self.assertEqual(self.eject([(1, err)]), 1)
        self.assertEqual(len(self.calls), 1, "never power off a card that is still mounted")
        self.assertIn("in use", self.events[-1]["message"])

    def test_power_off_failure_still_counts_as_ejected(self):
        self.assertEqual(self.eject([(0, ""), (1, "Error powering off drive: not supported")]), 0)
        self.assertFalse(self.events[-1]["poweredOff"])
        self.assertIn("not supported", self.events[-1]["note"])

    def test_no_card(self):
        self.assertEqual(dji.eject(emit=self.events.append, mounts_text="", sysroot=str(self.fx.sys),
                                   run=self.fake_run([])), 1)
        self.assertEqual(self.calls, [])

    def test_parent_disk(self):
        part = self.fx.sys / "devices/pci0/usb5/5-1/5-1:1.0/host0/block/sda/sda1"
        part.mkdir()
        (part / "partition").write_text("1")
        (self.fx.sys / "class/block/sda1").symlink_to(part)
        self.assertEqual(dji.parent_disk("/dev/sda1", str(self.fx.sys)), "/dev/sda")
        self.assertEqual(dji.parent_disk("/dev/sda", str(self.fx.sys)), "/dev/sda")


class LibraryTests(unittest.TestCase):
    """An import folder that already exists, possibly organized by hand."""

    def setUp(self):
        self.fx = Fixture()
        self.events = []
        self.lib = self.fx.home / "Videos/OsmoAction4"

    def tearDown(self):
        self.fx.close()

    def run_import(self, **kw):
        return dji.run_import(emit=self.events.append, **self.fx.kw(), **kw)

    def done(self):
        return [e for e in self.events if e["event"] == "done"][-1]

    def test_reorganized_files_count_as_imported(self):
        v = self.fx.add("DJI_20261115115741_0001_D.MP4", b"v" * 100)
        trip = self.lib / "Moab trip/raw"
        trip.mkdir(parents=True)
        (trip / v.name).write_bytes(b"v" * 100)
        (self.lib / "notes.txt").write_text("mine")
        card = dji.status(**self.fx.kw())["cards"][0]
        self.assertTrue(card["destExists"])
        self.assertEqual(card["newFiles"], 0)
        self.assertEqual(self.run_import(), 0)
        self.assertEqual(self.done()["copied"], 0)
        self.assertFalse((self.lib / "2026-11-15").exists(), "no duplicate copy")
        self.assertEqual((self.lib / "notes.txt").read_text(), "mine")

    def test_move_verifies_against_the_reorganized_copy(self):
        v = self.fx.add("DJI_20261115115741_0001_D.MP4", b"v" * 100)
        trip = self.lib / "Moab trip"
        trip.mkdir(parents=True)
        (trip / v.name).write_bytes(b"v" * 100)
        self.assertEqual(self.run_import(move=True), 0)
        self.assertFalse(v.exists())
        self.assertEqual(self.done()["deleted"], 1)

    def test_same_name_different_size_elsewhere_is_copied_alongside(self):
        v = self.fx.add("DJI_20261115115741_0001_D.MP4", b"v" * 100)
        (self.lib / "old").mkdir(parents=True)
        (self.lib / "old" / v.name).write_bytes(b"other")
        self.assertEqual(self.run_import(), 0)
        self.assertEqual((self.lib / "old" / v.name).read_bytes(), b"other")
        self.assertEqual((self.lib / "2026-11-15" / v.name).read_bytes(), b"v" * 100)

    def test_hidden_part_files_do_not_count(self):
        v = self.fx.add("DJI_20261115115741_0001_D.MP4", b"v" * 100)
        day = self.lib / "2026-11-15"
        day.mkdir(parents=True)
        (day / f".{v.name}.part").write_bytes(b"v" * 100)
        self.assertEqual(dji.status(**self.fx.kw())["cards"][0]["newFiles"], 1)

    def test_dest_on_the_card_is_refused(self):
        self.fx.add("DJI_20261115115741_0001_D.MP4")
        dest = str(self.fx.mount / "backup")
        self.assertEqual(self.run_import(dest_template=dest), 1)
        self.assertIn("on the card", self.events[-1]["message"])
        self.assertIn("on the card", dji.status(dest_template=dest, **self.fx.kw())["cards"][0]["destError"])

    def test_dest_through_a_file_is_refused(self):
        self.fx.add("DJI_20261115115741_0001_D.MP4")
        (self.fx.home / "Videos").write_text("not a folder")
        self.assertEqual(self.run_import(), 1)
        self.assertIn("not a folder", self.events[-1]["message"])

    @unittest.skipIf(os.geteuid() == 0, "root ignores permissions")
    def test_unwritable_dest_is_refused(self):
        self.fx.add("DJI_20261115115741_0001_D.MP4")
        self.lib.mkdir(parents=True)
        self.lib.chmod(0o555)
        try:
            self.assertEqual(self.run_import(), 1)
            self.assertIn("not writable", self.events[-1]["message"])
        finally:
            self.lib.chmod(0o755)

    def test_symlinked_library_is_followed(self):
        v = self.fx.add("DJI_20261115115741_0001_D.MP4", b"v" * 100)
        nas = Path(self.fx.tmp.name) / "nas"
        (nas / "2026-11-15").mkdir(parents=True)
        (nas / "2026-11-15" / v.name).write_bytes(b"v" * 100)
        (self.fx.home / "Videos").mkdir()
        self.lib.symlink_to(nas)
        self.assertEqual(dji.status(**self.fx.kw())["cards"][0]["newFiles"], 0)


class WatchTests(unittest.TestCase):
    """The bar must follow changes that do not come with a mount event."""

    def run_watch(self, states, sigs, loops, refresh=5.0):
        import argparse
        import io

        now = [0.0]
        out = io.StringIO()
        it_states = iter(states)
        it_sigs = iter(sigs)

        def get_status():
            value = next(it_states)
            if isinstance(value, Exception):
                raise value
            return value

        def sleep(sec):
            now[0] += sec

        args = argparse.Namespace(interval=2.0, refresh=refresh)
        dji.watch(args, get_status=get_status, signature=lambda: next(it_sigs), sleep=sleep,
                  clock=lambda: now[0], out=out, loops=loops)
        return [json.loads(line) for line in out.getvalue().splitlines()]

    def card(self, new):
        return {"cards": [{"mount": "/m", "newFiles": new}], "unmounted": [], "error": None}

    def test_rescans_while_card_present(self):
        # Card stays mounted (same signature); a CLI import drops newFiles 10 -> 0.
        states = [self.card(10), self.card(10), self.card(0), self.card(0)]
        lines = self.run_watch(states, ["a"] * 12, loops=12)
        self.assertEqual([line["cards"][0]["newFiles"] for line in lines], [10, 0])

    def test_no_rescans_without_a_card(self):
        empty = {"cards": [], "unmounted": [], "error": None}
        lines = self.run_watch([empty], ["a"] * 10, loops=10)  # a second status() call would raise
        self.assertEqual(len(lines), 1)

    def test_mount_change_rescans(self):
        empty = {"cards": [], "unmounted": [], "error": None}
        lines = self.run_watch([empty, self.card(10)], ["a", "a", "b"], loops=3)
        self.assertEqual([len(line["cards"]) for line in lines], [0, 1])

    def test_vanished_device_does_not_kill_the_watcher(self):
        states = [OSError("gone"), self.card(3)]
        lines = self.run_watch(states, ["a", "a"], loops=2)
        self.assertEqual(lines[0]["cards"][0]["newFiles"], 3)


class ModelCacheTests(unittest.TestCase):
    def test_model_is_sniffed_once_per_file(self):
        fx = Fixture(usb_product=None)
        try:
            fx.add("DJI_20261115115741_0001_D.MP4", mp4_with_model())
            calls = []
            original = dji.sniff_model
            dji.sniff_model = lambda path: calls.append(path) or original(path)
            try:
                for _ in range(3):
                    self.assertEqual(dji.status(**fx.kw())["cards"][0]["model"], "OsmoAction4")
            finally:
                dji.sniff_model = original
            self.assertEqual(len(calls), 1)
        finally:
            fx.close()


class SecurityTests(unittest.TestCase):
    """Regression tests for the pre-marketplace security audit."""

    def setUp(self):
        self.fx = Fixture()
        self.events = []

    def tearDown(self):
        self.fx.close()

    def lib_day(self):
        return self.fx.home / "Videos/OsmoAction4/2026-11-15"

    # -- only removable devices count as cards

    def fixed_disk(self, removable="0"):
        disk = self.fx.sys / "devices/pci0/nvme/nvme0/nvme0n1"
        disk.mkdir(parents=True)
        (disk / "removable").write_text(removable)
        (self.fx.sys / "class/block/nvme0n1").symlink_to(disk)
        return self.fx.mounts.replace("/dev/sda ", "/dev/nvme0n1 ")

    def test_fixed_disk_is_never_a_card(self):
        self.fx.add("DJI_20261115115741_0001_D.MP4")
        mounts = self.fixed_disk()
        self.assertEqual(dji.find_cards(mounts, str(self.fx.sys)), [])
        self.assertEqual(dji.eject(emit=self.events.append, mounts_text=mounts, sysroot=str(self.fx.sys),
                                   run=lambda *a, **k: self.fail("must not run udisksctl")), 1)

    def test_removable_flag_counts(self):
        self.fx.add("DJI_20261115115741_0001_D.MP4")
        self.assertEqual(len(dji.find_cards(self.fixed_disk("1"), str(self.fx.sys))), 1)

    def test_non_dev_sources_are_ignored(self):
        self.fx.add("DJI_20261115115741_0001_D.MP4")
        mounts = self.fx.mounts.replace("/dev/sda ", "nas:/share ")
        self.assertEqual(dji.find_cards(mounts, str(self.fx.sys)), [])

    # -- card-supplied text

    def test_markup_in_usb_product_and_label_is_neutralized(self):
        fx = Fixture(usb_product='<img src="https://example.invalid/t">Osmo&Action4')
        try:
            fx.add("DJI_20261115115741_0001_D.MP4")
            evil = fx.mount.parent / "<i>SD&Card\"x"
            fx.mount.rename(evil)
            fx.mounts = fx.mounts.replace(str(fx.mount).replace(" ", "\\040"), str(evil))
            card = dji.status(**fx.kw())["cards"][0]
            for field in ("model", "label", "folder"):
                self.assertFalse(set(card[field]) & set("<>&\"'"), (field, card[field]))
            self.assertEqual(card["label"], "iSDCardx")
        finally:
            fx.close()

    def test_unmounted_product_is_neutralized(self):
        fx = Fixture(usb_product="Osmo\x1b[31m<script>")
        try:
            state = dji.status(mounts_text="", sysroot=str(fx.sys), home=str(fx.home))
            self.assertEqual(state["unmounted"], ["Osmo[31mscript"])
        finally:
            fx.close()

    def test_clean_label_caps_length(self):
        self.assertEqual(len(dji.clean_label("A" * 500)), 48)

    # -- symlinks

    def test_symlink_on_card_is_ignored(self):
        secret = Path(self.fx.tmp.name) / "secret.txt"
        secret.write_text("private")
        (self.fx.folder / "DJI_20261115115741_0001_D.MP4").symlink_to(secret)
        card = dji.status(**self.fx.kw())["cards"][0]
        self.assertEqual(card["newFiles"], 0)
        dji.run_import(emit=self.events.append, move=True, **self.fx.kw())
        self.assertFalse(self.lib_day().exists())
        self.assertEqual(secret.read_text(), "private")

    def test_symlinked_dji_folder_is_ignored(self):
        elsewhere = Path(self.fx.tmp.name) / "elsewhere"
        elsewhere.mkdir()
        (elsewhere / "DJI_20261115115741_0001_D.MP4").write_bytes(b"x")
        (self.fx.mount / "DCIM/DJI_002").symlink_to(elsewhere)
        self.assertEqual(dji.dji_folders(self.fx.mount), [self.fx.folder])

    def test_planted_part_symlink_cannot_redirect_the_write(self):
        v = self.fx.add("DJI_20261115115741_0001_D.MP4", b"video" * 20)
        victim = Path(self.fx.tmp.name) / "victim.txt"
        victim.write_text("keep me")
        day = self.lib_day()
        day.mkdir(parents=True)
        (day / f".{v.name}.part").symlink_to(victim)
        self.assertEqual(dji.run_import(emit=self.events.append, **self.fx.kw()), 0)
        self.assertEqual(victim.read_text(), "keep me")
        self.assertEqual((day / v.name).read_bytes(), b"video" * 20)

    def test_symlinked_date_folder_is_refused(self):
        v = self.fx.add("DJI_20261115115741_0001_D.MP4")
        outside = Path(self.fx.tmp.name) / "outside"
        outside.mkdir()
        self.lib_day().parent.mkdir(parents=True)
        self.lib_day().symlink_to(outside)
        self.assertEqual(dji.run_import(emit=self.events.append, move=True, **self.fx.kw()), 1)
        self.assertEqual(list(outside.iterdir()), [])
        self.assertTrue(v.exists(), "a refused copy must never delete the original")

    def test_date_folder_swapped_mid_copy_cannot_redirect_the_write(self):
        # HANCORE-linux review of f727d6b: the date folder was checked, then
        # looked up by path again, so a co-writer of a shared library could
        # swap it for a symlink in between.
        v = self.fx.add("DJI_20261115115741_0001_D.MP4", b"v" * (dji.CHUNK * 3))
        outside = Path(self.fx.tmp.name) / "outside"
        outside.mkdir()
        day = self.lib_day()
        moved = day.with_name("moved")
        swapped = []

        def swap(n):
            if not swapped:
                day.rename(moved)
                day.symlink_to(outside)
                swapped.append(True)

        dji.copy_file(v, day / v.name, swap, verify=True)
        self.assertTrue(swapped)
        self.assertEqual(list(outside.iterdir()), [])
        self.assertEqual((moved / v.name).read_bytes(), v.read_bytes())
        self.assertEqual(sorted(p.name for p in moved.iterdir()), [v.name])

    def test_date_folder_that_is_a_file_is_refused(self):
        v = self.fx.add("DJI_20261115115741_0001_D.MP4")
        self.lib_day().parent.mkdir(parents=True)
        self.lib_day().write_text("not a folder")
        with self.assertRaises(OSError):
            dji.copy_file(v, self.lib_day() / v.name, lambda n: None, verify=False)
        self.assertEqual(self.lib_day().read_text(), "not a folder")

    def test_open_nofollow_refuses_symlinks(self):
        real = Path(self.fx.tmp.name) / "real"
        real.write_bytes(b"x")
        link = Path(self.fx.tmp.name) / "link"
        link.symlink_to(real)
        with self.assertRaises(OSError):
            dji.hash_file(link)

    # -- bounded parsing

    def test_mp4_box_walk_is_bounded(self):
        import time

        with tempfile.NamedTemporaryFile(suffix=".MP4", delete=False) as f:
            f.write((b"\0\0\0\x08free") * 200_000)  # 200k empty boxes, no moov
        try:
            start = time.monotonic()
            self.assertEqual(dji.mp4_moov(f.name), b"")
            self.assertLess(time.monotonic() - start, 1.0)
        finally:
            os.unlink(f.name)

    def test_card_scan_is_bounded(self):
        original = dji.MAX_CARD_FILES
        dji.MAX_CARD_FILES = 5
        try:
            for i in range(10):
                self.fx.add(f"DJI_202611151157{i:02d}_00{i:02d}_D.MP4")
            self.assertLessEqual(len(dji.scan([self.fx.folder], self.fx.home / "lib")), 5)
        finally:
            dji.MAX_CARD_FILES = original

    # -- subprocesses

    def test_udisksctl_runs_by_absolute_path_with_fixed_env(self):
        import subprocess

        seen = {}

        def run(cmd, **kw):
            seen.update(cmd=cmd, env=kw.get("env"))
            return subprocess.CompletedProcess(cmd, 0, "", "")

        dji.udisks("unmount", "-b", "/dev/sda", run=run)
        self.assertEqual(seen["cmd"][0], "/usr/bin/udisksctl")
        self.assertEqual(seen["env"], {"PATH": "/usr/bin:/bin", "LANG": "C.UTF-8"})


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
