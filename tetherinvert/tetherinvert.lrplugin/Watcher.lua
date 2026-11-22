--[[ The hot folder itself.

Every two seconds: read the request TetherInvert left, list the folders it names (a roll's
scans/ and prints/), and add each TIFF that is new and has stopped changing. A file counts as finished once two passes in a
row see the same size and date (TetherInvert also writes under another name and renames, so a
.tif never appears half-written).

Each file's TetherInvert metadata (TetherInvert Film, TetherInvert Scan, in its XMP) goes into the
plug-in's own fields (Metadata.lua): for a file it adds, right after adding it; for a file
Lightroom knows already that has changed (metadata edited in TetherInvert, a frame rendered again),
when the change is seen. Of the standard fields, those the SDK lets a plug-in set (caption,
creator, copyright) are refreshed too, and the keywords are kept in step through Lightroom's
keywords; camera, lens, exposure and capture time only Lightroom's Read Metadata from Files reads.

State is kept in the global tetherinvertHotFolder so the shutdown script and the menu commands
see the same table as the running loop. ]]

local LrApplication = import 'LrApplication'
local LrDate = import 'LrDate'
local LrDialogs = import 'LrDialogs'
local LrFileUtils = import 'LrFileUtils'
local LrPathUtils = import 'LrPathUtils'
local LrTasks = import 'LrTasks'

local Watcher = {}

Watcher.VERSION = '0.4.0'
Watcher.INTERVAL = 2 -- seconds between passes
Watcher.MAX_AGE = 12 * 3600 -- a request older than this is not picked up when Lightroom starts
Watcher.MAX_LOG = 200000 -- bytes

local home = LrPathUtils.getStandardFilePath('home')
local exchange = LrPathUtils.child(LrPathUtils.child(home, '.tetherinvert'), 'lightroom')
Watcher.folder = exchange
Watcher.requestFile = LrPathUtils.child(exchange, 'request.txt')
Watcher.statusFile = LrPathUtils.child(exchange, 'status.txt')
Watcher.logFile = LrPathUtils.child(exchange, 'plugin.log')

tetherinvertHotFolder = tetherinvertHotFolder or {}
local state = tetherinvertHotFolder
state.generation = state.generation or 0
state.pending = state.pending or {} -- path -> size/date seen once
state.seen = state.seen or {} -- path -> size/date dealt with
state.added = state.added or 0
state.failed = state.failed or 0
Watcher.state = state

-- Lightroom's file functions may yield inside a task, and a plain pcall cannot be yielded
-- across. So: LrTasks.pcall in a task, pcall in a menu command.
local function try(func, ...)
  if LrTasks.canYield() then
    return LrTasks.pcall(func, ...)
  end
  return pcall(func, ...)
end

local function write(path, text, mode)
  try(function()
    LrFileUtils.createAllDirectories(exchange)
    local file = io.open(path, mode)
    if file then
      file:write(text)
      file:close()
    end
  end)
end

function Watcher.log(text)
  local stamp = ''
  try(function()
    stamp = LrDate.timeToUserFormat(LrDate.currentTime(), '%Y-%m-%d %H:%M:%S')
  end)
  write(Watcher.logFile, stamp .. '  ' .. tostring(text) .. '\n', 'a')
end

-- ----- TetherInvert's metadata in a TIFF -----

-- Each plug-in field (Metadata.lua) and where TetherInvert writes it: namespace prefix, property.
Watcher.FIELDS = {
  { 'filmStock', 'tetherinvertFilm', 'Stock' },
  { 'filmFormat', 'tetherinvertFilm', 'Format' },
  { 'boxSpeed', 'tetherinvertFilm', 'BoxSpeed' },
  { 'exposureIndex', 'tetherinvertFilm', 'ExposureIndex' },
  { 'developer', 'tetherinvertFilm', 'Developer' },
  { 'dateDeveloped', 'tetherinvertFilm', 'DateDeveloped' },
  { 'dateShotTo', 'tetherinvertFilm', 'DateShotTo' },
  { 'scanRoll', 'tetherinvertScan', 'Roll' },
  { 'scanFrame', 'tetherinvertScan', 'Frame' },
  { 'scanFile', 'tetherinvertScan', 'File' },
  { 'scanCamera', 'tetherinvertScan', 'Camera' },
  { 'scanLight', 'tetherinvertScan', 'Light' },
  { 'scanPreset', 'tetherinvertScan', 'Preset' },
  { 'scanExposure', 'tetherinvertScan', 'Exposure' },
  { 'scanFlatField', 'tetherinvertScan', 'FlatField' },
  { 'scanTime', 'tetherinvertScan', 'Scanned' },
  { 'scanBase', 'tetherinvertScan', 'Base' },
  { 'scanCorrections', 'tetherinvertScan', 'Corrections' },
  { 'scanSoftware', 'tetherinvertScan', 'Software' },
}
-- The standard fields photo:setRawMetadata accepts, and their XMP property.
Watcher.STANDARD = { { 'caption', 'dc:description' }, { 'creator', 'dc:creator' }, { 'copyright', 'dc:rights' } }
Watcher.SCAN_NS = 'http://ns.tetherinvert.org/scan/1.0/'
local XMP_TAG = 700

local function unsigned(bytes, little)
  local value = 0
  if little then
    for i = #bytes, 1, -1 do
      value = value * 256 + string.byte(bytes, i)
    end
  else
    for i = 1, #bytes do
      value = value * 256 + string.byte(bytes, i)
    end
  end
  return value
end

-- The XMP packet of a TIFF (tag 700 of its first IFD), or nil. Reads the header, the IFD and
-- the packet only: a flat TIFF is over 100 MB. Classic TIFF in either byte order; TetherInvert
-- writes no BigTIFF.
function Watcher.readXmp(path)
  local file = io.open(path, 'rb')
  if not file then
    return nil
  end
  local function at(offset, size)
    file:seek('set', offset)
    return file:read(size) or ''
  end
  local packet
  local header = at(0, 8)
  local order = string.sub(header, 1, 2)
  local little = order == 'II'
  if #header == 8 and (little or order == 'MM') and unsigned(string.sub(header, 3, 4), little) == 42 then
    local ifd = unsigned(string.sub(header, 5, 8), little)
    local count = unsigned(at(ifd, 2), little)
    local entries = at(ifd + 2, count * 12)
    for i = 0, count - 1 do
      local entry = string.sub(entries, i * 12 + 1, i * 12 + 12)
      local kind = unsigned(string.sub(entry, 3, 4), little)
      if #entry == 12 and unsigned(string.sub(entry, 1, 2), little) == XMP_TAG and (kind == 1 or kind == 7) then
        local size = unsigned(string.sub(entry, 5, 8), little) -- BYTE or UNDEFINED: one byte each
        packet = size <= 4 and string.sub(entry, 9, 8 + size) or at(unsigned(string.sub(entry, 9, 12), little), size)
        break
      end
    end
  end
  file:close()
  return packet
end

local ENTITIES = { lt = '<', gt = '>', quot = '"', apos = "'", amp = '&' }

local function unescape(text)
  return (string.gsub(text, '&(#?x?)(%w+);', function(kind, name)
    if kind == '' then
      return ENTITIES[name]
    end
    local code = tonumber(name, kind == '#x' and 16 or 10)
    return code and code < 128 and string.char(code) or nil -- other characters TetherInvert writes as UTF-8, not as references
  end))
end

-- A property as TetherInvert writes it (an element), or as a program that rewrote the packet may
-- have (an attribute of the description).
local function property(packet, name)
  local value = string.match(packet, '<' .. name .. '>(.-)</' .. name .. '>')
    or string.match(packet, '[%s"]' .. name .. '="(.-)"')
  if value then
    local item = string.match(value, '<rdf:li[^>]*>(.-)</rdf:li>') -- a list: its first item
    return unescape(item or value)
  end
  return nil
end

-- The items of a list property (dc:subject): every rdf:li, unescaped.
local function items(packet, name)
  local list = {}
  local value = string.match(packet, '<' .. name .. '>(.-)</' .. name .. '>')
  for item in string.gmatch(value or '', '<rdf:li[^>]*>(.-)</rdf:li>') do
    list[#list + 1] = unescape(item)
  end
  return list
end

-- What a file says: { fields = plug-in field -> value, standard = raw key -> value, keywords =
-- list }, or nil for a file without TetherInvert's metadata.
function Watcher.parse(packet)
  if not packet or not string.find(packet, Watcher.SCAN_NS, 1, true) then
    return nil
  end
  local found = { fields = {}, standard = {}, keywords = items(packet, 'dc:subject') }
  for _, field in ipairs(Watcher.FIELDS) do
    found.fields[field[1]] = property(packet, field[2] .. ':' .. field[3])
  end
  for _, field in ipairs(Watcher.STANDARD) do
    found.standard[field[1]] = property(packet, field[2])
  end
  return found
end

-- Keywords are compared by name, as Lightroom does: case does not count.
local function lower(name)
  return string.lower(name or '')
end

-- The keywords to add and to take away for the photo to have the file's: TetherInvert's that it
-- lacks; of those the plug-in set before (keywordsSet), the ones TetherInvert has dropped since.
-- Keywords from anywhere else are left alone.
local function keywordChanges(photo, found, list)
  local present, wanted = {}, {}
  for _, keyword in ipairs(photo:getRawMetadata('keywords') or {}) do
    present[lower(keyword:getName())] = keyword
  end
  for _, name in ipairs(found.keywords) do
    wanted[lower(name)] = true
    if not present[lower(name)] then
      list[#list + 1] = { add = name }
    end
  end
  local before = photo:getPropertyForPlugin(_PLUGIN, 'keywordsSet', nil, true)
  for name in string.gmatch(before or '', '[^\n]+') do
    if not wanted[lower(name)] and present[lower(name)] then
      list[#list + 1] = { remove = present[lower(name)], name = name }
    end
  end
  local now = #found.keywords > 0 and table.concat(found.keywords, '\n') or nil -- keywords never hold a line break
  if before ~= now then
    list[#list + 1] = { plugin = 'keywordsSet', value = now }
  end
end

-- What has to be set on a photo for it to show what its file says: the plug-in fields that
-- differ (one the file no longer has is cleared), the keywords, and with `standard` the standard
-- fields the file has and the photo shows otherwise. A field TetherInvert left empty does not clear
-- one typed in Lightroom.
local function changes(photo, found, standard)
  local list = {}
  for _, field in ipairs(Watcher.FIELDS) do
    local value = found.fields[field[1]]
    if photo:getPropertyForPlugin(_PLUGIN, field[1], nil, true) ~= value then
      list[#list + 1] = { plugin = field[1], value = value }
    end
  end
  keywordChanges(photo, found, list)
  if standard then
    for _, field in ipairs(Watcher.STANDARD) do
      local value = found.standard[field[1]]
      if value and photo:getFormattedMetadata(field[1]) ~= value then
        list[#list + 1] = { raw = field[1], value = value }
      end
    end
  end
  return list
end

local function apply(photo, list) -- inside catalog write access
  local catalog = LrApplication.activeCatalog()
  for _, change in ipairs(list) do
    if change.plugin then
      photo:setPropertyForPlugin(_PLUGIN, change.plugin, change.value)
    elseif change.add then -- a keyword at the top level, the one there if there is one (returnExisting)
      photo:addKeyword(catalog:createKeyword(change.add, {}, true, nil, true))
    elseif change.remove then
      photo:removeKeyword(change.remove)
    else
      photo:setRawMetadata(change.raw, change.value)
    end
  end
end

-- The menu command: every photo's fields from its file, in one write. Counts updated, current
-- (nothing to change), skipped (no TetherInvert metadata); busy when the catalog was locked.
function Watcher.readPhotos(photos)
  local done, work = { updated = 0, current = 0, skipped = 0 }, {}
  for _, photo in ipairs(photos) do
    local found = Watcher.parse(Watcher.readXmp(photo:getRawMetadata('path')))
    if not found then
      done.skipped = done.skipped + 1
    else
      local list = changes(photo, found, true)
      if #list > 0 then
        work[#work + 1] = { photo = photo, list = list }
      else
        done.current = done.current + 1
      end
    end
  end
  if #work > 0 then
    local catalog = LrApplication.activeCatalog()
    local outcome = catalog:withWriteAccessDo('Read TetherInvert metadata', function()
      for _, item in ipairs(work) do
        apply(item.photo, item.list)
      end
    end, { timeout = 30 })
    if outcome == 'queued' or outcome == 'aborted' then
      done.busy = true
      return done
    end
    done.updated = #work
  end
  Watcher.log('read metadata of ' .. tostring(#photos) .. ' photos: ' .. done.updated .. ' updated, '
    .. done.current .. ' current, ' .. done.skipped .. ' without TetherInvert metadata')
  return done
end

local function bezel(text)
  try(function()
    LrDialogs.showBezel(text)
  end)
end

-- What TetherInvert reads to know the request arrived: state, files added, plug-in version, the
-- token of the request this answers, then one watched folder per line.
local function writeStatus()
  local lines = { state.folders and 'watching' or 'idle', tostring(state.added), Watcher.VERSION, state.token or '' }
  for _, folder in ipairs(state.folders or {}) do
    lines[#lines + 1] = folder
  end
  local text = table.concat(lines, '\n') .. '\n'
  if text ~= state.status then
    state.status = text
    write(Watcher.statusFile, text, 'w')
  end
end

-- The request: a token that changes with every request, then one folder per line.
-- No folder means: stop watching.
function Watcher.readRequest()
  if LrFileUtils.exists(Watcher.requestFile) ~= 'file' then
    return nil, {}
  end
  local text = LrFileUtils.readFile(Watcher.requestFile) or ''
  local lines = {}
  for line in string.gmatch(text, '[^\r\n]+') do
    lines[#lines + 1] = line
  end
  local token = table.remove(lines, 1)
  return token, lines
end

-- The counter keeps two requests in the same millisecond apart: with the same token, the second
-- would be taken as answered already (the tests' menu commands, one right after the other).
function Watcher.writeRequest(folder)
  state.requests = (state.requests or 0) + 1
  local token = 'lightroom-' .. tostring(math.floor(LrDate.currentTime() * 1000)) .. '-' .. tostring(state.requests)
  write(Watcher.requestFile, token .. '\n' .. (folder and (folder .. '\n') or ''), 'w')
end

local function requestAge()
  local attributes = LrFileUtils.fileAttributes(Watcher.requestFile) or {}
  if not attributes.fileModificationDate then
    return 0
  end
  return LrDate.currentTime() - attributes.fileModificationDate
end

-- "Roll15/scans and Roll15/prints": the leaf names alone are the same for every roll.
local function names(folders)
  local short = {}
  for _, folder in ipairs(folders) do
    short[#short + 1] = LrPathUtils.leafName(LrPathUtils.parent(folder)) .. '/' .. LrPathUtils.leafName(folder)
  end
  return table.concat(short, ' and ')
end

-- The folder all watched folders are in, when there are several: the roll of scans/ and prints/.
local function commonParent(folders)
  local parent = #folders > 1 and LrPathUtils.parent(folders[1])
  for _, folder in ipairs(folders) do
    if LrPathUtils.parent(folder) ~= parent then
      return nil
    end
  end
  return parent
end

-- Show the watched folders in the Library. Lightroom keeps a roll's folder above its scans/ and
-- prints/ (Scans > Roll15 > scans, seen on the rig), so the roll itself is shown when the catalog
-- has it: with Show Photos in Subfolders, both appear, the prints too once they come. Otherwise
-- the folders it knows by now, again when one more becomes known: the prints folder may get its
-- first file long after the scans.
local function showFolders()
  if not state.folders or state.shown >= #state.folders then
    return
  end
  try(function()
    local catalog = LrApplication.activeCatalog()
    local parent = commonParent(state.folders)
    local roll = parent and catalog:getFolderByPath(parent)
    if roll then
      if catalog:setActiveSources({ roll }) then
        state.shown = #state.folders
      end
      return
    end
    local known = {}
    for _, folder in ipairs(state.folders) do
      known[#known + 1] = catalog:getFolderByPath(folder)
    end
    if #known > state.shown and catalog:setActiveSources(known) then
      state.shown = #known
    end
  end)
end

function Watcher.watch(folders)
  state.pending, state.seen, state.added, state.failed, state.shown = {}, {}, 0, 0, 0
  local watching = {}
  for _, folder in ipairs(folders) do
    if LrFileUtils.exists(folder) == 'directory' then
      watching[#watching + 1] = folder
      Watcher.log('watching ' .. folder)
    else
      Watcher.log('cannot watch, not a folder: ' .. folder)
    end
  end
  if #watching > 0 then
    bezel('TetherInvert: watching ' .. names(watching))
  elseif #folders > 0 then
    bezel('TetherInvert: folder not found')
  elseif state.folders then
    Watcher.log('stopped watching ' .. table.concat(state.folders, ', '))
    bezel('TetherInvert: stopped watching')
  end
  state.folders = #watching > 0 and watching or nil
  showFolders()
end

-- Returns 'added', 'refreshed' (already in the catalog, its TetherInvert fields brought up to
-- date with the file), 'known' (already in the catalog, nothing to change) or 'busy' (the
-- catalog was locked). Lightroom reads the standard fields of a file it adds by itself.
local function add(path)
  local catalog = LrApplication.activeCatalog()
  local found = Watcher.parse(Watcher.readXmp(path))
  local known = catalog:findPhotoByPath(path)
  local list = known and found and changes(known, found, true) or {}
  if known and #list == 0 then
    return 'known'
  end
  local outcome = catalog:withWriteAccessDo(known and 'Refresh TetherInvert metadata' or 'Add TetherInvert scan', function()
    if known then
      apply(known, list)
    else
      local photo = catalog:addPhoto(path)
      if found then
        apply(photo, changes(photo, found, false))
      end
    end
  end, { timeout = 30 })
  if outcome == 'queued' or outcome == 'aborted' then
    return 'busy'
  end
  return known and 'refreshed' or 'added'
end

local function isTiff(path)
  local extension = string.lower(LrPathUtils.extension(path) or '')
  local name = LrPathUtils.leafName(path) or ''
  return (extension == 'tif' or extension == 'tiff') and string.sub(name, 1, 1) ~= '.'
end

local function scan()
  local ready = {}
  for _, folder in ipairs(state.folders) do
    if LrFileUtils.exists(folder) == 'directory' then
      for path in LrFileUtils.files(folder) do -- this folder only: the RAW files in raw/ stay out
        if isTiff(path) then
          local attributes = LrFileUtils.fileAttributes(path) or {}
          local stamp = tostring(attributes.fileSize) .. '/' .. tostring(attributes.fileModificationDate)
          if state.seen[path] ~= stamp then
            if state.pending[path] == stamp then
              ready[#ready + 1] = { path = path, stamp = stamp }
            else
              state.pending[path] = stamp
            end
          end
        end
      end
    end
  end
  table.sort(ready, function(a, b)
    return a.path < b.path
  end)
  for _, file in ipairs(ready) do
    local ok, outcome = LrTasks.pcall(add, file.path)
    local name = LrPathUtils.leafName(file.path)
    if ok and outcome == 'busy' then
      Watcher.log('catalog busy, will try again: ' .. name)
    else
      state.seen[file.path], state.pending[file.path] = file.stamp, nil
      if not ok then
        state.failed = state.failed + 1
        Watcher.log('could not add ' .. name .. ': ' .. tostring(outcome))
        bezel('TetherInvert: could not add ' .. name)
      elseif outcome == 'added' then
        state.added = state.added + 1
        Watcher.log('added ' .. name)
        bezel('TetherInvert: added ' .. name)
        showFolders()
      elseif outcome == 'refreshed' then
        Watcher.log('metadata refreshed: ' .. name)
      else
        Watcher.log('already in the catalog: ' .. name)
      end
    end
  end
end

-- One pass. Called by the loop; also what the tests drive.
function Watcher.pass()
  local token, folders = Watcher.readRequest()
  if token and token ~= state.token then
    state.token = token
    if not state.started and #folders > 0 and requestAge() > Watcher.MAX_AGE then
      Watcher.log('not resuming an old request for ' .. table.concat(folders, ', '))
    else
      Watcher.watch(folders)
    end
  end
  state.started = true
  if state.folders then
    scan()
  end
  writeStatus()
end

function Watcher.run()
  state.generation = state.generation + 1
  local mine = state.generation
  state.running, state.started, state.token, state.status = true, false, nil, nil
  LrTasks.startAsyncTaskWithoutErrorHandler(function()
    local size = (LrFileUtils.fileAttributes(Watcher.logFile) or {}).fileSize
    if size and size > Watcher.MAX_LOG then
      write(Watcher.logFile, '', 'w')
    end
    Watcher.log('plug-in ' .. Watcher.VERSION .. ' started')
    while state.running and state.generation == mine do
      local ok, problem = LrTasks.pcall(Watcher.pass)
      if not ok and problem ~= state.problem then -- say it once, not every two seconds
        state.problem = problem
        Watcher.log('error: ' .. tostring(problem))
      end
      LrTasks.sleep(Watcher.INTERVAL)
    end
    Watcher.log('plug-in stopped')
  end, 'TetherInvert hot folder')
end

function Watcher.stop()
  state.running = false
end

return Watcher
