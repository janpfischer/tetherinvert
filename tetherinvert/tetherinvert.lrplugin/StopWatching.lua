-- Menu command: stop watching. A request without a folder means exactly that.
local Watcher = require 'Watcher'
Watcher.writeRequest(nil)
