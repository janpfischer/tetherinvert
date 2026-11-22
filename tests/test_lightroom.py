"""The Lightroom Classic hot folder.

The plug-in's Lua runs here for real (Lua 5.1, as in Lightroom), against a stand-in for the
Lightroom SDK that follows the SDK's documented behaviour. What this cannot show is how
Lightroom itself behaves; that needs the real application.
"""

from __future__ import annotations

import os
import sys
import time
from pathlib import Path

import pytest

from tetherinvert import lightroom

COCOA = 978307200  # seconds between 1970 and 2001: Lightroom counts from 2001

SDK = r"""
local py = ...
bezels, messages, sleeps, limit, chosen, in_task = {}, {}, 0, 3, nil, false

_PLUGIN = { id = 'org.tetherinvert.hotfolder' }
catalog = { photos = {}, order = {}, busy = false, broken = {}, sources = nil, gates = 0, parents = false, writing = false, selected = {}, shown = {} }
function catalog:findPhotoByPath(path) return self.photos[path] end
function catalog:withWriteAccessDo(name, func, timeout)
  assert(type(name) == 'string' and timeout.timeout > 0)
  if self.busy then return 'aborted' end
  self.gates = self.gates + 1
  self.writing = true
  local ok, problem = pcall(func)
  self.writing = false
  if not ok then error(problem, 0) end
  return 'executed'
end
-- As in Lightroom: the selection when it is more than one photo, else every photo shown.
function catalog:getMultipleSelectedOrAllPhotos()
  if #self.selected > 1 then return self.selected end
  return self.shown
end

-- A photo: its plug-in fields (only those Metadata.lua defines) and the standard fields a plug-in may set.
fields_defined, settable = {}, { caption = true, creator = true, copyright = true }
Photo = {}
Photo.__index = Photo
function Photo:getRawMetadata(key)
  assert(key == 'path' or key == 'keywords', 'raw metadata ' .. key)
  if key == 'path' then return self.path end
  local list = {}
  for _, keyword in ipairs(self.keywords) do list[#list + 1] = keyword end
  return list
end
-- Keywords: one object per name at the top level (createKeyword with returnExisting), a photo's in a list.
Keyword = {}
Keyword.__index = Keyword
function Keyword:getName() return self.name end
catalog.keywords = {}
function catalog:createKeyword(name, synonyms, includeOnExport, parent, returnExisting)
  assert(self.writing, 'createKeyword outside write access'); assert(returnExisting == true and parent == nil)
  for _, keyword in ipairs(self.keywords) do
    if string.lower(keyword.name) == string.lower(name) then return keyword end
  end
  local keyword = setmetatable({ name = name }, Keyword)
  self.keywords[#self.keywords + 1] = keyword
  return keyword
end
function Photo:addKeyword(keyword)
  assert(catalog.writing, 'addKeyword outside write access'); assert(getmetatable(keyword) == Keyword)
  for _, had in ipairs(self.keywords) do if had == keyword then return end end
  self.keywords[#self.keywords + 1] = keyword
end
function Photo:removeKeyword(keyword)
  assert(catalog.writing, 'removeKeyword outside write access'); assert(getmetatable(keyword) == Keyword)
  for i, had in ipairs(self.keywords) do if had == keyword then table.remove(self.keywords, i); return end end
end
function Photo:getPropertyForPlugin(plugin, id, version, noThrow)
  assert(in_task, 'getPropertyForPlugin outside a task'); assert(plugin == _PLUGIN and fields_defined[id], 'no such field: ' .. tostring(id))
  return self.props[id]
end
function Photo:setPropertyForPlugin(plugin, id, value)
  assert(catalog.writing, 'setPropertyForPlugin outside write access'); assert(plugin == _PLUGIN and fields_defined[id], 'no such field: ' .. tostring(id))
  assert(value == nil or type(value) == 'string')
  self.props[id] = value
end
function Photo:getFormattedMetadata(key) return self.raw[key] end
function Photo:setRawMetadata(key, value)
  assert(catalog.writing, 'setRawMetadata outside write access'); assert(settable[key], 'not settable: ' .. key)
  self.raw[key] = value
end
function catalog:addPhoto(path)
  assert(self.writing, 'addPhoto outside write access')
  if self.broken[path] then error('unsupported file') end
  assert(py.exists(path) == 'file', 'no such file')
  self.photos[path] = setmetatable({ path = path, props = {}, raw = {}, keywords = {} }, Photo)
  self.order[#self.order + 1] = py.leaf(path)
  return self.photos[path]
end
-- A folder is known once it holds a photo; with `parents`, so are the folders above it, as in a
-- catalog that already has a folder above the rolls (Scans > Roll15 > scans, seen on the rig).
function catalog:getFolderByPath(path)
  for known in pairs(self.photos) do
    local folder = py.parent(known)
    if folder == path or (self.parents and py.parent(folder) == path) then return { path = path } end
  end
  return nil
end
function catalog:setActiveSources(sources)
  self.sources = {}
  for i, source in ipairs(sources) do self.sources[i] = source.path end
  return true
end

local modules = {
  LrApplication = { activeCatalog = function() return catalog end },
  LrDate = { currentTime = py.now, timeToUserFormat = function(when, format) return 'now' end },
  LrDialogs = {
    showBezel = function(text) bezels[#bezels + 1] = text end,
    message = function(text, info, style) messages[#messages + 1] = text .. '|' .. info end,
    runOpenPanel = function(args) assert(args.canChooseDirectories); return chosen end,
  },
  LrFileUtils = {
    exists = py.exists, readFile = py.read, fileAttributes = py.attributes, createAllDirectories = py.mkdirs,
    files = function(folder)
      local list, index = py.files(folder), 0
      return function() index = index + 1; return list[index] end
    end,
  },
  LrPathUtils = {
    getStandardFilePath = function(which) assert(which == 'home'); return py.home end,
    child = function(path, name) return path .. '/' .. name end,
    extension = py.extension, leafName = py.leaf, parent = py.parent,
  },
  LrTasks = {
    pcall = function(func, ...) assert(in_task, 'LrTasks.pcall outside a task'); return pcall(func, ...) end,
    canYield = function() return in_task end,
    sleep = function(seconds)
      sleeps = sleeps + 1
      if sleeps >= limit then tetherinvertHotFolder.running = false end
    end,
    startAsyncTask = function(func) in_task = true; func(); in_task = false end,
    startAsyncTaskWithoutErrorHandler = function(func) in_task = true; func(); in_task = false end,
  },
}
function import(name) return assert(modules[name], 'no such SDK module: ' .. name) end
"""


class Lightroom:
    """A Lua 5.1 with the plug-in loaded and a stand-in SDK. `clock` moves its time."""

    def __init__(self, home: Path) -> None:
        lua51 = pytest.importorskip("lupa.lua51")
        self.lua, self.clock = lua51.LuaRuntime(unpack_returned_tuples=True), 0.0
        lua = self.lua

        def exists(path):
            return "file" if os.path.isfile(path) else "directory" if os.path.isdir(path) else False

        def attributes(path):
            if not os.path.exists(path):
                return lua.table_from({})
            info = os.stat(path)
            return lua.table_from({"fileSize": info.st_size, "fileModificationDate": info.st_mtime - COCOA})

        callbacks = {
            "home": str(home),
            "now": lambda: time.time() + self.clock - COCOA,
            "exists": exists,
            "read": lambda path: Path(path).read_text(encoding="utf-8"),
            "attributes": attributes,
            "mkdirs": lambda path: (os.makedirs(path, exist_ok=True), True)[1],
            "files": lambda folder: lua.table_from(sorted(str(p) for p in Path(folder).iterdir() if p.is_file())),
            "extension": lambda path: Path(path).suffix.lstrip("."),
            "leaf": lambda path: Path(path).name,
            "parent": lambda path: str(Path(path).parent),
        }
        lua.execute(f"package.path = {str(lightroom.PLUGIN)!r} .. '/?.lua;' .. package.path")
        lua.eval("function(source, callbacks) return assert(loadstring(source))(callbacks) end")(SDK, lua.table_from(callbacks))
        lua.execute(f"for _, field in ipairs(dofile({str(lightroom.PLUGIN / 'Metadata.lua')!r}).metadataFieldsForPhotos) do fields_defined[field.id] = true end")
        self.watcher = lua.eval("require 'Watcher'")

    def run(self, script: str):
        return self.lua.eval(f"dofile({str(lightroom.PLUGIN / script)!r})")

    def passes(self, count: int = 1) -> None:
        """Passes of the loop, made the way the loop makes them: inside a task."""
        self.lua.execute("in_task = true")
        try:
            for _ in range(count):
                self.watcher["pass"]()
        finally:
            self.lua.execute("in_task = false")

    def added(self) -> list[str]:
        return list(self.lua.eval("catalog.order").values())

    def bezels(self) -> list[str]:
        return list(self.lua.eval("bezels").values())

    def photo(self, path: Path):
        return self.lua.eval("function(path) return catalog.photos[path] end")(str(path))

    def fields(self, path: Path) -> dict:
        """A photo's plug-in fields and the standard fields set on it."""
        photo = self.photo(path)
        return {**dict(photo.props.items()), **{f"raw:{k}": v for k, v in photo.raw.items()}}

    def keywords(self, path: Path) -> list[str]:
        return [keyword.name for keyword in self.photo(path).keywords.values()]

    def sources(self) -> list[str]:
        """The folders the Library was last pointed at."""
        return list((self.lua.eval("catalog.sources") or {}).values())


@pytest.fixture
def home(tmp_path, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    (tmp_path / "home").mkdir()
    return tmp_path / "home"


def tiff(path: Path, size: int = 1000) -> Path:
    path.write_bytes(b"II*\x00" + bytes(size))
    return path


def test_manifest_names_files_that_exist():
    lua51 = pytest.importorskip("lupa.lua51")
    info = lua51.LuaRuntime().eval(f"dofile({str(lightroom.PLUGIN / 'Info.lua')!r})")
    scripts = [info.LrInitPlugin, info.LrShutdownPlugin]
    for menu in (info.LrLibraryMenuItems, info.LrExportMenuItems):
        scripts += [item.file for item in menu.values()]
        assert all(item.title.isascii() for item in menu.values())
    scripts += [info.LrMetadataProvider, info.LrMetadataTagsetFactory]
    assert len(scripts) == 12 and all((lightroom.PLUGIN / name).is_file() for name in scripts)
    assert info.LrForceInitPlugin is True and info.LrToolkitIdentifier == "org.tetherinvert.hotfolder"
    assert all(path.read_text(encoding="utf-8").isascii() for path in lightroom.PLUGIN.glob("*.lua"))


def test_the_plug_in_adds_finished_tiffs_where_they_are(home, tmp_path):
    roll = tmp_path / "Scans" / "Röll 01"
    scans, prints = roll / "scans", roll / "prints"
    for folder in (scans, prints, roll / "raw"):
        folder.mkdir(parents=True)
    lr = Lightroom(home)
    state = lr.watcher.state

    lr.passes()  # nothing asked for yet
    assert lightroom.status() == {"watching": False, "folders": [], "added": 0, "version": "0.4.0", "token": ""}

    token = lightroom.request([scans, prints])
    assert not lightroom.answered(token)
    lr.passes()
    assert lightroom.answered(token) and lightroom.status()["watching"] and lightroom.status()["folders"] == [str(scans), str(prints)]
    assert lr.bezels() == ["TetherInvert: watching Röll 01/scans and Röll 01/prints"]

    # Only finished TIFFs of the watched folders themselves: not the RAW files, not the notes, not a file still being written.
    tiff(scans / "Röll 01_001.tif")
    tiff(scans / "Röll 01_001_noflat.tif")
    tiff(roll / "raw" / "Röll 01_001_R.ARW")
    tiff(roll / "raw" / "stray.tif")
    tiff(roll / "stray.tif")
    tiff(scans / "Röll 01_002.tif.part")
    tiff(scans / ".hidden.tif")
    (roll / "roll.json").write_text("{}")
    lr.passes()
    assert lr.added() == []  # seen once: it might still be growing
    lr.passes()
    # In either order: the plug-in sorts with Lua's <, which follows the locale (Qt sets it, so after the window tests
    # "_noflat.tif" can come first).
    assert sorted(lr.added()) == ["Röll 01_001.tif", "Röll 01_001_noflat.tif"] and lightroom.status()["added"] == 2
    assert lr.sources() == [str(scans)]  # the Library shows the folder Lightroom knows so far
    lr.passes(3)
    assert len(lr.added()) == 2  # and nothing is added twice

    # The first print: added where it is, and the Library shows both folders of the roll.
    tiff(prints / "Röll 01_001.tif")
    lr.passes(2)
    assert lr.added()[-1] == "Röll 01_001.tif" and lr.lua.eval("function(path) return catalog.photos[path] ~= nil end")(str(prints / "Röll 01_001.tif"))
    assert lr.sources() == [str(scans), str(prints)]
    tiff(prints / "Röll 01_002.tif")
    lr.lua.execute("catalog.sources = nil")
    lr.passes(2)
    assert lr.sources() == []  # both are shown already: the Library is left where the operator put it


    # A file that changes between two looks is not finished.
    growing = tiff(scans / "Röll 01_002.tif", 10)
    lr.passes()
    tiff(growing, 5000)
    lr.passes()
    assert lr.added().count("Röll 01_002.tif") == 1  # the print of that name only
    lr.passes()
    assert lr.added().count("Röll 01_002.tif") == 2

    # Rendered again later: the photo is already in the catalog and stays one photo.
    tiff(growing, 6000)
    lr.passes(3)
    assert lr.added().count("Röll 01_002.tif") == 2 and "already in the catalog" in (home / ".tetherinvert/lightroom/plugin.log").read_text()

    # The catalog is busy (an import, a backup): try again on the next pass.
    lr.lua.execute("catalog.busy = true")
    tiff(scans / "Röll 01_003.tif")
    lr.passes(3)
    assert "Röll 01_003.tif" not in lr.added()
    lr.lua.execute("catalog.busy = false")
    lr.passes()
    assert lr.added()[-1] == "Röll 01_003.tif"

    # A file Lightroom refuses is reported once, not hammered every two seconds.
    bad = tiff(scans / "Röll 01_004.tif")
    lr.lua.eval("function(path) catalog.broken[path] = true end")(str(bad))
    lr.passes(5)
    log = (home / ".tetherinvert/lightroom/plugin.log").read_text()
    assert log.count("could not add Röll 01_004.tif") == 1 and state.failed == 1 and len(lr.added()) == 6

    # Stop: a request without a folder ends the watch of both.
    stop = lightroom.request()
    lr.passes()
    assert lightroom.answered(stop) and lightroom.status() == {"watching": False, "folders": [], "added": 0, "version": "0.4.0", "token": stop}
    assert lr.bezels()[-1] == "TetherInvert: stopped watching"
    tiff(scans / "Röll 01_005.tif")
    tiff(prints / "Röll 01_005.tif")
    lr.passes(3)
    assert len(lr.added()) == 6

    # One folder alone, as outside a session (the prints).
    alone = lightroom.request([prints])
    lr.passes(3)
    assert lightroom.answered(alone) and lightroom.status()["folders"] == [str(prints)] and lr.added()[-1] == "Röll 01_005.tif"
    assert "Röll 01_005.tif" not in [Path(p).name for p in lr.lua.eval("catalog.photos") if Path(p).parent == scans]

    # A folder that is not there is said so; the others are still watched.
    gone = lightroom.request([tmp_path / "Scans" / "Nowhere", prints])
    lr.passes()
    assert lightroom.answered(gone) and lightroom.status()["folders"] == [str(prints)] and "not a folder" in (home / ".tetherinvert/lightroom/plugin.log").read_text()
    gone = lightroom.request([tmp_path / "Scans" / "Nowhere"])
    lr.passes()
    assert lightroom.answered(gone) and not lightroom.status()["watching"] and lr.bezels()[-1] == "TetherInvert: folder not found"


def test_the_library_shows_the_roll_when_the_catalog_has_its_folder(home, tmp_path):
    """Lightroom keeps the folders above a roll's scans/ and prints/ (Scans > Roll15 > scans, seen on
    the rig). Shown as the roll, both folders appear, and the prints come into view by themselves."""
    roll = tmp_path / "Scans" / "Roll 02"
    for folder in (roll / "scans", roll / "prints"):
        folder.mkdir(parents=True)
    lr = Lightroom(home)
    lr.lua.execute("catalog.parents = true")
    lightroom.request([roll / "scans", roll / "prints"])
    tiff(roll / "scans" / "Roll 02_001.tif")
    lr.passes(3)
    assert lr.sources() == [str(roll)]  # from the first scan on
    lr.lua.execute("catalog.sources = nil")
    tiff(roll / "prints" / "Roll 02_001.tif")
    lr.passes(3)
    assert lr.sources() == [] and lr.added() == ["Roll 02_001.tif", "Roll 02_001.tif"]  # not pointed again
    lightroom.request([roll / "prints"])  # one folder alone is shown as itself, not as its roll
    lr.passes()
    assert lr.sources() == [str(roll / "prints")]


def test_the_loop_starts_stops_and_leaves_yesterdays_request_alone(home, tmp_path):
    roll = tmp_path / "Roll"
    roll.mkdir()
    tiff(roll / "Roll_001.tif")
    request_file = home / ".tetherinvert" / "lightroom" / "request.txt"

    # Lightroom starts a day after the last session: the old request is not picked up again.
    lightroom.request([roll])
    old = time.time() - 13 * 3600
    os.utime(request_file, (old, old))
    lr = Lightroom(home)
    lr.run("Init.lua")  # three passes, then the stand-in ends the loop
    assert lr.added() == [] and not lightroom.status()["watching"]
    log = (home / ".tetherinvert/lightroom/plugin.log").read_text()
    assert "plug-in 0.4.0 started" in log and "not resuming an old request" in log and "plug-in stopped" in log

    # A request made now is, even when Lightroom was not running when it was made.
    token = lightroom.request([roll])
    lr = Lightroom(home)
    lr.run("Init.lua")
    assert lr.added() == ["Roll_001.tif"] and lightroom.answered(token)

    # Reloading the plug-in: the shutdown script ends the running loop.
    lr.watcher.state.running = True
    lr.run("Shutdown.lua")
    assert lr.watcher.state.running is False

    # The menu commands leave the same kind of request the button does.
    other = tmp_path / "Other"
    other.mkdir()
    lr.lua.eval("function(path) chosen = { path } end")(str(other))
    lr.run("WatchFolder.lua")
    assert request_file.read_text().split("\n")[1] == str(other)
    lr.passes()
    assert lightroom.status()["folders"] == [str(other)]
    lr.run("ShowStatus.lua")
    assert list(lr.lua.eval("messages").values())[-1].startswith("The TetherInvert hot folder is not running")
    lr.watcher.state.running = True
    lr.run("ShowStatus.lua")
    assert f"Watching {other}|Added since the watch began: 0" in list(lr.lua.eval("messages").values())[-1]
    lr.run("StopWatching.lua")
    lr.passes()
    assert not lightroom.status()["watching"]


def test_installing_the_plug_in_and_pressing_the_button(home, tmp_path, monkeypatch):
    modules = tmp_path / "Modules"
    assert lightroom.plugin_state(modules) == "missing"
    path, did = lightroom.install_plugin(modules)
    assert did == "installed" and path == modules / "tetherinvert.lrplugin" and (path / "Info.lua").is_file()
    assert sorted(p.name for p in path.iterdir()) == sorted(p.name for p in lightroom.PLUGIN.glob("*.lua"))
    assert lightroom.install_plugin(modules)[1] == "current"
    (path / "Watcher.lua").write_text("-- an older version")
    assert lightroom.plugin_state(modules) == "outdated" and lightroom.install_plugin(modules)[1] == "updated"
    assert lightroom.plugin_state(modules) == "current"

    if sys.platform != "darwin":  # the real thing is for macOS; elsewhere it says so
        with pytest.raises(lightroom.LightroomError, match="macOS only"):
            lightroom.open_hot_folder([tmp_path / "Roll" / "scans"])

    launched = []
    monkeypatch.setattr(lightroom, "modules_folder", lambda: tmp_path / "Fresh")
    monkeypatch.setattr(lightroom, "launch", lambda: launched.append(1))
    roll = tmp_path / "Scans" / "Roll"
    token, notes = lightroom.open_hot_folder([roll / "scans", roll / "prints"])
    assert launched == [1] and (roll / "scans").is_dir() and (roll / "prints").is_dir() and (tmp_path / "Fresh" / "tetherinvert.lrplugin" / "Init.lua").is_file()
    assert "was installed" in notes[0] and "restart it once" in notes[0] and "asked to watch Roll/scans and Roll/prints" in notes[1]
    assert (home / ".tetherinvert/lightroom/request.txt").read_text() == f"{token}\n{(roll / 'scans').resolve()}\n{(roll / 'prints').resolve()}\n"
    with pytest.raises(lightroom.LightroomError, match="at least one folder"):
        lightroom.open_hot_folder([])
    token, notes = lightroom.open_hot_folder([roll / "prints"])
    assert launched == [1, 1] and len(notes) == 1 and not lightroom.answered(token)
    assert "Plug-in Manager" in lightroom.no_answer() and "plugin.log" in lightroom.no_answer()


# ----- TetherInvert's metadata in the files -----


def tetherinvert_tiff(path: Path, **fields) -> Path:
    """A TIFF as TetherInvert writes it, with roll metadata and a frame note."""
    import numpy as np

    from tetherinvert import metadata, tiffio
    from tetherinvert.session import Frame, Roll

    fields = {"film": "Ilford HP5 Plus", "ei": "800", "developer": "Ilfosol 3 & <stop>", "keywords": "berlin, lake", **fields}
    roll = Roll(path.parent.parent, metadata=metadata.updated({}, fields))
    frame = Frame(7, [], "hp5", "white", [0.0] * 3, [0.1] * 4, light="Scanlight v4a (fw 2)", metadata={"note": "Anna on the jetty", "keywords": ["A & B"]})
    path.parent.mkdir(parents=True, exist_ok=True)
    tiffio.write_tiff(path, np.zeros((8, 8, 3), np.uint16), 2.2, "test", xmp=metadata.xmp(roll, frame, "flat"))
    return path


def test_the_plug_in_reads_tag_700_from_either_byte_order(home, tmp_path):
    import tifffile

    lr = Lightroom(home)
    read = lambda path: lr.watcher.readXmp(str(path))  # noqa: E731
    written = tetherinvert_tiff(tmp_path / "Röll" / "scans" / "Röll_007.tif")
    with tifffile.TiffFile(written) as tif:
        packet = bytes(tif.pages[0].tags[700].value)
        assert tif.byteorder == "<"
    assert read(written) == packet.decode("utf-8")  # lupa hands Lua's bytes over as UTF-8 text
    big = tmp_path / "big.tif"
    tifffile.imwrite(big, tifffile.imread(written), byteorder=">", extratags=[(700, 1, len(packet), packet, True)], metadata=None)
    assert tifffile.TiffFile(big).byteorder == ">" and read(big) == packet.decode("utf-8")
    plain = tmp_path / "plain.tif"
    tifffile.imwrite(plain, tifffile.imread(written))
    assert read(plain) is None and read(tmp_path / "missing.tif") is None and read(tiff(tmp_path / "junk.tif")) is None

    found = lr.watcher.parse(read(written))
    fields = dict(found.fields.items())
    assert fields["filmStock"] == "Ilford HP5 Plus" and fields["exposureIndex"] == "800" and fields["developer"] == "Ilfosol 3 & <stop>"
    assert fields["scanFrame"] == "7" and fields["scanRoll"] == "Röll" and fields["scanLight"] == "Scanlight v4a (fw 2), white LED"
    assert "boxSpeed" not in fields and found.standard["caption"] == "Anna on the jetty" and list(found.keywords.values()) == ["berlin", "lake", "A & B"]
    assert lr.watcher.parse(None) is None and lr.watcher.parse("<x:xmpmeta>no TetherInvert here</x:xmpmeta>") is None
    # Rewritten by another program, with the properties as attributes: still read.
    attributes = '<rdf:Description xmlns:tetherinvertScan="http://ns.tetherinvert.org/scan/1.0/" tetherinvertScan:Roll="R&amp;1" tetherinvertScan:Frame="00"/>'
    assert dict(lr.watcher.parse(attributes).fields.items()) == {"scanRoll": "R&1", "scanFrame": "00"}


def test_the_plug_in_sets_its_fields_when_it_adds_a_file_and_when_the_file_changes(home, tmp_path):
    from tetherinvert.tiffio import set_xmp

    scans = tmp_path / "Roll15" / "scans"
    first = tetherinvert_tiff(scans / "Roll15_007.tif")
    other = tiff(scans / "Roll15_008.tif")  # no TetherInvert metadata
    lr = Lightroom(home)
    lightroom.request([scans])
    lr.passes(2)
    assert sorted(lr.added()) == ["Roll15_007.tif", "Roll15_008.tif"]
    fields = lr.fields(first)
    assert fields["filmStock"] == "Ilford HP5 Plus" and fields["scanFrame"] == "7" and "boxSpeed" not in fields
    assert "raw:caption" not in fields  # Lightroom reads the standard fields of a file it adds by itself
    assert lr.keywords(first) == ["berlin", "lake", "A & B"] and fields["keywordsSet"] == "berlin\nlake\nA & B"
    # A keyword given in Lightroom, and one of TetherInvert's taken off there: TetherInvert's own next change decides.
    lr.lua.eval("""function(photo)
      catalog.writing = true
      photo:addKeyword(catalog:createKeyword('mine', {}, true, nil, true))
      photo:removeKeyword(catalog:createKeyword('lake', {}, true, nil, true))
      catalog.writing = false
    end""")(lr.photo(first))
    assert lr.fields(other) == {}

    # Changed in TetherInvert: the file's XMP is replaced, the photo's fields follow, and one cleared goes.
    gates = lr.lua.eval("catalog.gates")
    from tetherinvert import metadata
    from tetherinvert.session import Frame, Roll

    roll = Roll(tmp_path / "Roll15", metadata={"film": "Kodak Tri-X", "box_speed": "400", "author": "Jan", "keywords": ["Berlin", "lake", "water"]})
    packet = metadata.xmp(roll, Frame(7, [], "hp5", "white", [0.0] * 3, [0.1, 0.1, 0.1, 0.1], metadata={"note": "new note", "keywords_left_out": ["lake"]}), "flat")
    set_xmp(first, packet)
    lr.passes(2)
    fields = lr.fields(first)
    assert fields["filmStock"] == "Kodak Tri-X" and fields["boxSpeed"] == "400" and "exposureIndex" not in fields and "developer" not in fields
    assert fields["raw:caption"] == "new note" and fields["raw:creator"] == "Jan" and "raw:copyright" not in fields
    # TetherInvert's keywords now: Berlin (berlin, the same keyword), water; "A & B" dropped in TetherInvert goes; "mine" stays.
    assert lr.keywords(first) == ["berlin", "mine", "water"] and fields["keywordsSet"] == "Berlin\nwater"
    assert lr.added().count("Roll15_007.tif") == 1 and "metadata refreshed: Roll15_007.tif" in (home / ".tetherinvert/lightroom/plugin.log").read_text()
    assert lr.lua.eval("catalog.gates") == gates + 1
    # Watched again, nothing changed: no write to the catalog at all.
    lightroom.request([scans])
    lr.passes(3)
    assert lr.lua.eval("catalog.gates") == gates + 1


def test_reading_metadata_by_menu_for_photos_imported_any_way(home, tmp_path):
    folder = tmp_path / "Elsewhere" / "scans"
    with_it, plain = tetherinvert_tiff(folder / "A.tif"), tiff(tmp_path / "B.tif")
    lr = Lightroom(home)
    add = lr.lua.eval("""function(path)
      catalog.writing = true; local photo = catalog:addPhoto(path); catalog.writing = false
      catalog.selected[#catalog.selected + 1] = photo
    end""")
    for path in (with_it, plain):
        add(str(path))
    lr.run("ReadMetadata.lua")
    message = list(lr.lua.eval("messages").values())[-1]
    assert message.startswith("TetherInvert: read metadata of 2 photos") and "Updated: 1" in message and "skipped: 1" in message
    assert "Camera, lens, exposure and capture time: Metadata > Read Metadata from Files" in message
    assert lr.fields(with_it)["filmStock"] == "Ilford HP5 Plus" and lr.fields(with_it)["raw:caption"] == "Anna on the jetty" and lr.fields(plain) == {}
    assert lr.keywords(with_it) == ["berlin", "lake", "A & B"] and lr.keywords(plain) == []
    lr.run("ReadMetadata.lua")
    assert "Updated: 0\nAlready up to date: 1" in list(lr.lua.eval("messages").values())[-1]
    lr.lua.execute("catalog.busy = true")
    lr.lua.eval("function(photo) photo.props.filmStock = 'stale' end")(lr.photo(with_it))
    lr.run("ReadMetadata.lua")
    assert list(lr.lua.eval("messages").values())[-1].startswith("TetherInvert: the catalog is busy")


def test_reading_metadata_by_menu_with_one_photo_selected_reads_every_photo_shown(home, tmp_path):
    """Clicking a folder leaves one photo selected: the command reads the whole view, here a
    roll's scans and prints shown together (Show Photos in Subfolders), not that one photo."""
    scan, printed = tetherinvert_tiff(tmp_path / "Roll15" / "scans" / "A.tif"), tetherinvert_tiff(tmp_path / "Roll15" / "prints" / "A.tif")
    lr = Lightroom(home)
    add = lr.lua.eval("""function(path)
      catalog.writing = true; local photo = catalog:addPhoto(path); catalog.writing = false
      catalog.shown[#catalog.shown + 1] = photo
      return photo
    end""")
    first = add(str(scan))
    add(str(printed))
    lr.lua.eval("function(photo) catalog.selected = { photo } end")(first)
    lr.run("ReadMetadata.lua")
    assert list(lr.lua.eval("messages").values())[-1].startswith("TetherInvert: read metadata of 2 photos")
    assert lr.fields(scan)["filmStock"] == "Ilford HP5 Plus" and lr.fields(printed)["filmStock"] == "Ilford HP5 Plus"


def test_the_plug_in_fields_python_and_lua_agree():
    """Metadata.lua defines the fields, Tagset.lua shows them, Watcher.lua reads them where
    metadata.py writes them."""
    from tetherinvert import metadata

    lua51 = pytest.importorskip("lupa.lua51")
    lua = lua51.LuaRuntime()
    defined = [f.id for f in lua.eval(f"dofile({str(lightroom.PLUGIN / 'Metadata.lua')!r})").metadataFieldsForPhotos.values()]
    shown = [item for item in lua.eval(f"dofile({str(lightroom.PLUGIN / 'Tagset.lua')!r})")["items"].values() if isinstance(item, str)]
    shown = [item.rsplit(".", 1)[1] for item in shown if item.startswith("org.tetherinvert.hotfolder.")]
    lua.execute(f"package.path = {str(lightroom.PLUGIN)!r} .. '/?.lua;' .. package.path")
    lua.execute("function import(name) return setmetatable({}, { __index = function() return function() return '/nowhere' end end }) end")
    read = [tuple(field.values()) for field in lua.eval("require 'Watcher'").FIELDS.values()]
    assert defined[-1] == "keywordsSet"  # bookkeeping, not from a TetherInvert namespace: not in the panel's set
    assert defined[:-1] == shown == [field[0] for field in read]
    written = [("tetherinvertFilm", name) for name, _key in metadata.FILM] + [("tetherinvertScan", name) for name in metadata.SCAN]
    assert [(prefix, name) for _id, prefix, name in read] == written
