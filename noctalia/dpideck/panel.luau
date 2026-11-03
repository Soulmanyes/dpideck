--!nonstrict
-- DPIdeck panel: the mouse's settings, opened by clicking the bar widget.
--
-- control.py does the HID work with DPIdeck's atk.py and prints the settings
-- as JSON: a DPI block plus sections of drop-downs and switches, already
-- limited to what this mouse offers. Every change runs `control.py set KEY
-- VALUE`, which reads the mouse, writes, and reads it back, so the panel
-- always ends up showing what the mouse really stored.
--
-- Changes queue up and run one at a time. The panel shows each change right
-- away and takes the mouse's answer once the queue is empty.

local TIMEOUT_MS = 45000 -- a sleeping mouse takes a while to give up

local data = nil -- last settings from control.py
local busy = false
local queue = {} -- pending { key, value } writes, oldest first
local status = nil -- { text, color }
local drafts = {} -- slider positions while dragging, by slider key

-- Rows outside DPI and quick settings show by default and can be hidden from
-- the panel itself: the header's edit button turns on edit mode, where each
-- of these rows gets an eye button. Row key -> group; the light's three rows
-- hide together. Hidden groups are saved in the plugin's data folder, which
-- survives restarts and plugin updates.
local OPTIONAL = {
  lod = "lift_off",
  debounce = "debounce",
  sleep = "sleep",
  light_mode = "lighting",
  light_brightness = "lighting",
  light_speed = "lighting",
}
local hidden = {} -- group -> true
local editing = false

local function prefsPath()
  local dir = noctalia.pluginDataDir()
  return dir and (dir .. "/panel.json") or nil
end

local function loadHidden()
  hidden = {}
  -- a missing or broken panel.json just means nothing is hidden
  local ok, prefs = pcall(function()
    local path = prefsPath()
    local raw = path and noctalia.readFile(path)
    return raw and noctalia.json.decode(raw)
  end)
  if ok and type(prefs) == "table" and type(prefs.hidden) == "table" then
    for _, group in ipairs(prefs.hidden) do
      hidden[group] = true
    end
  end
end

local function saveHidden()
  local list = {}
  for group in pairs(hidden) do
    table.insert(list, group)
  end
  table.sort(list)
  local path = prefsPath()
  if path then
    noctalia.writeFile(path, noctalia.json.encode({ hidden = list }))
  end
end

local function isHidden(item)
  local group = OPTIONAL[item.key]
  return group ~= nil and hidden[group] == true
end
local closeButton = true
local radiusScale = 1.0

local render

local function tr(key, vars)
  return noctalia.tr(key, vars)
end

local function script()
  return noctalia.pluginDir() .. "/control.py"
end

-- ---- talking to control.py ------------------------------------------------

local run

local function handle(result, saving)
  busy = false
  local out = nil
  if type(result) == "table" and not result.timedOut and result.exitCode == 0 then
    out = noctalia.json.decode(result.stdout or "")
  end
  if type(out) ~= "table" then
    local why = (type(result) == "table" and result.timedOut) and tr("panel.timeout")
      or (type(result) == "table" and noctalia.string.trim(result.stderr or "") ~= "" and result.stderr)
      or tr("panel.failed")
    status = { text = why, color = "error" }
    queue = {}
  elseif out.asleep then
    status = { text = tr("panel.asleep"), color = "error" }
    queue = {} -- don't keep waking a mouse the user isn't touching
  elseif out.error ~= nil then
    status = { text = (saving and (tr("panel.not-saved") .. ": ") or "") .. tostring(out.error), color = "error" }
    queue = {}
  else
    -- Only take the mouse's view once nothing else is pending, so a later
    -- queued change doesn't flicker back to its old value in between.
    if #queue == 0 then
      data = out
    end
    status = out.message and { text = out.message, color = "primary" } or nil
  end
  if #queue > 0 then
    run()
  else
    if status ~= nil and status.color == "error" and saving then
      -- Show what the mouse really has after a failed write, keeping the error.
      run("state", true)
      return
    end
    render()
  end
end

run = function(mode, keepStatus)
  if busy then
    return
  end
  local argv
  local saving = false
  if mode == "state" or #queue == 0 then
    argv = { "python3", "-I", script(), "state" }
  else
    local job = table.remove(queue, 1)
    local encoded = noctalia.json.encode(job.value)
    argv = { "python3", "-I", script(), "set", job.key, encoded }
    saving = true
  end
  busy = true
  local kept = keepStatus and status or nil
  if not keepStatus then
    status = { text = saving and tr("panel.saving") or tr("panel.reading"), color = "on_surface_variant" }
  end
  render()
  noctalia.runAsync(argv, function(result)
    handle(result, saving)
    if kept ~= nil and status == nil then
      status = kept
      render()
    end
  end, TIMEOUT_MS)
end

local function change(key, value)
  -- A newer value for the same setting replaces one still waiting.
  for _, job in ipairs(queue) do
    if job.key == key and key ~= "stage_dpi" then
      job.value = value
      return
    end
  end
  table.insert(queue, { key = key, value = value })
  run()
end

-- ---- optimistic updates ---------------------------------------------------

local function findItem(key)
  if data == nil then
    return nil
  end
  for _, section in ipairs(data.sections or {}) do
    for _, item in ipairs(section.items or {}) do
      if item.key == key then
        return item
      end
    end
  end
  return nil
end

local function setItem(key, value)
  local item = findItem(key)
  if item ~= nil then
    item.value = value
  end
  if key == "light_mode" then
    local b, s = findItem("light_brightness"), findItem("light_speed")
    if b then
      b.enabled = value ~= "off"
    end
    if s then
      s.enabled = value == "breathing"
    end
  end
end

-- ---- view -------------------------------------------------------------------

local function textBlock(title, subtitle)
  local children = { ui.label({ text = title, color = "on_surface" }) }
  if subtitle ~= nil and subtitle ~= "" then
    table.insert(children, ui.label({ text = subtitle, fontSize = 11, color = "on_surface_variant" }))
  end
  return ui.column({ gap = 2, flexGrow = 1 }, children)
end

local function choiceRow(item)
  local labels, selected = {}, 0
  for i, option in ipairs(item.options) do
    table.insert(labels, option.label)
    if option.value == item.value then
      selected = i - 1
    end
  end
  return ui.row({ key = "row-" .. item.key, gap = 12, align = "center" }, {
    textBlock(item.title, item.subtitle),
    ui.select({
      key = "select-" .. item.key,
      options = labels,
      selectedIndex = selected,
      width = 160,
      controlSize = "sm",
      enabled = item.enabled ~= false,
      onChange = function(index)
        local option = item.options[(tonumber(index) or 0) + 1]
        if option == nil or option.value == item.value then
          return
        end
        setItem(item.key, option.value)
        change(item.key, option.value)
        render()
      end,
    }),
  })
end

local function toggleRow(item)
  return ui.row({ key = "row-" .. item.key, gap = 12, align = "center" }, {
    textBlock(item.title, item.subtitle),
    ui.toggle({
      key = "toggle-" .. item.key,
      checked = item.value == true,
      enabled = item.enabled ~= false,
      onChange = function(value)
        local on = value == "true"
        if on == item.value then
          return
        end
        setItem(item.key, on)
        change(item.key, on)
        render()
      end,
    }),
  })
end

-- A row of buttons over a few fixed values (polling rate, sensor mode); the
-- current one is filled, and the tooltip has the full name.
local function stepsRow(item)
  local buttons = {}
  for i, o in ipairs(item.options) do
    local selected = o.value == item.value
    table.insert(buttons, ui.button({
      -- selection is in the key so the button is rebuilt with its new look
      key = "steps-" .. item.key .. "-" .. i .. (selected and "-on" or ""),
      text = o.label,
      tooltip = o.name,
      variant = selected and "primary" or "outline",
      controlSize = "sm",
      flexGrow = 1,
      enabled = item.enabled ~= false,
      onClick = function()
        if o.value ~= item.value then
          setItem(item.key, o.value)
          change(item.key, o.value)
          render()
        end
      end,
    }))
  end
  return ui.column({ key = "row-" .. item.key, gap = 8 }, {
    textBlock(item.title, item.subtitle),
    ui.row({ gap = 6 }, buttons),
  })
end

-- DPI sliders move over a list of round stops, one slider position each, so
-- every stop can be reached by dragging: every 50 DPI up to 2000, then 100 up
-- to 5000, 250 up to 10000 and 1000 above. A straight 50-36000 track would put
-- every usual setting (400-3200) in its first tenth, and a smooth log track
-- skips values (one pixel is more than 50 DPI). Type a value for anything else.
local BANDS = { { 2000, 50 }, { 5000, 100 }, { 10000, 250 }, { math.huge, 1000 } }
local stopsCache = {}

local function dpiStops(lo, hi, step)
  local id = lo .. "-" .. hi .. "-" .. tostring(step)
  if stopsCache[id] then
    return stopsCache[id]
  end
  local grid = math.max(step or 1, 1) -- the sensor's own step; stops must be multiples of it
  local stops, last = { lo }, lo
  for _, band in ipairs(BANDS) do
    local upto, every = band[1], math.max(band[2], grid)
    local v = (math.floor(last / every) + 1) * every
    while v <= math.min(upto, hi) do
      local snapped = math.floor(v / grid + 0.5) * grid
      if snapped > last and snapped <= hi then
        table.insert(stops, snapped)
        last = snapped
      end
      v += every
    end
    if last >= hi then
      break
    end
  end
  if last < hi then
    table.insert(stops, hi)
  end
  stopsCache[id] = stops
  return stops
end

-- 0-based slider position of the stop nearest to dpi
local function dpiToPos(dpi, stops)
  local best, bestGap = 1, math.huge
  for i, s in ipairs(stops) do
    local gap = math.abs(s - dpi)
    if gap < bestGap then
      best, bestGap = i, gap
    end
  end
  return best - 1
end

local function heading(text)
  return ui.label({ text = text, fontWeight = "bold", color = "primary" })
end

local function card(children)
  return ui.column({
    gap = 10,
    padding = 12,
    fill = "surface_container",
    radius = 10 * radiusScale,
  }, children)
end

local function dpiSection()
  local dpi = data.dpi
  local rows = {}
  if dpi.stages == nil then
    table.insert(rows, ui.label({ text = tr("panel.dpi-unknown"), color = "on_surface_variant" }))
    return { heading(tr("panel.dpi")), card(rows) }
  end

  for i = 1, math.min(dpi.count, #dpi.stages) do
    local stored = dpi.stages[i]
    local sliderKey = "dpi-slider-" .. i
    local value = drafts[sliderKey] or stored
    local index = i - 1
    local active = index == dpi.active
    local stops = dpiStops(dpi.min, dpi.max, dpi.step)
    table.insert(rows, ui.row({ key = "stage-" .. i, gap = 8, align = "center" }, {
      ui.button({
        key = "stage-btn-" .. i .. (active and "-on" or ""),
        text = tr("panel.stage", { n = i }),
        variant = active and "primary" or "outline",
        controlSize = "sm",
        width = 96,
        tooltip = active and tr("panel.stage-active") or tr("panel.stage-activate"),
        onClick = function()
          if dpi.active ~= index then
            dpi.active = index
            change("active_stage", index)
            render()
          end
        end,
      }),
      ui.slider({
        key = sliderKey,
        min = 0,
        max = #stops - 1,
        step = 1,
        value = dpiToPos(value, stops), -- the draft while dragging
        controlSize = "sm",
        flexGrow = 1,
        onChange = function(v)
          local pos = tonumber(v)
          if pos ~= nil then
            drafts[sliderKey] = stops[math.clamp(math.floor(pos + 0.5), 0, #stops - 1) + 1]
            render()
          end
        end,
        onDragEnd = function()
          local n = drafts[sliderKey]
          drafts[sliderKey] = nil
          if n ~= nil and n ~= stored then
            dpi.stages[i] = n -- the mouse's answer replaces it, rounded as stored
            change("stage_dpi", { index, n })
          end
          render()
        end,
      }),
      ui.input({
        -- the value is in the key: a new reading (or a slider move) reseeds the field
        key = "dpi-" .. i .. "-" .. tostring(value),
        value = tostring(value),
        controlSize = "sm",
        width = 76,
        placeholder = tostring(dpi.min) .. "–" .. tostring(dpi.max),
        onSubmit = function(text)
          local n = tonumber(noctalia.string.trim(text))
          if n == nil then
            status = { text = tr("panel.dpi-number"), color = "error" }
            render()
            return
          end
          n = math.floor(n + 0.5)
          if n == stored then
            return
          end
          dpi.stages[i] = n
          change("stage_dpi", { index, n })
          render()
        end,
      }),
    }))
  end
  table.insert(rows, ui.label({
    text = tr("panel.dpi-hint", { min = dpi.min, max = dpi.max }),
    fontSize = 11,
    color = "on_surface_variant",
  }))

  if dpi.count_editable then
    local counts = {}
    for n = 1, 8 do
      table.insert(counts, tostring(n))
    end
    table.insert(rows, ui.row({ gap = 12, align = "center" }, {
      textBlock(tr("panel.stage-count"), nil),
      ui.select({
        key = "select-stage-count",
        options = counts,
        selectedIndex = dpi.count - 1,
        width = 160,
        controlSize = "sm",
        onChange = function(index)
          local n = (tonumber(index) or 0) + 1
          if n ~= dpi.count then
            dpi.count = n
            if dpi.active >= n then
              dpi.active = n - 1
            end
            change("stage_count", n)
            render()
          end
        end,
      }),
    }))
  end
  return { heading(tr("panel.dpi")), card(rows) }
end

local function body()
  local children = {}
  if data == nil then
    return children
  end
  for _, node in ipairs(dpiSection()) do
    table.insert(children, node)
  end
  if editing then
    table.insert(children, ui.label({
      text = tr("panel.edit-hint"),
      fontSize = 11,
      color = "on_surface_variant",
    }))
  end
  for _, section in ipairs(data.sections or {}) do
    local rows = {}
    for _, item in ipairs(section.items or {}) do
      local group = OPTIONAL[item.key]
      if isHidden(item) and not editing then
        continue
      end
      local row
      if isHidden(item) then
        -- edit mode: a hidden row is one line, its title and the eye to bring it back
        row = ui.row({ key = "row-" .. item.key .. "-hidden", gap = 12, align = "center" }, {
          ui.label({ text = item.title, color = "on_surface_variant", flexGrow = 1 }),
        })
      elseif item.type == "toggle" then
        row = toggleRow(item)
      elseif item.type == "steps" then
        row = stepsRow(item)
      else
        row = choiceRow(item)
      end
      if editing and group ~= nil then
        local isOff = hidden[group] == true
        row = ui.row({ key = "edit-" .. item.key .. (isOff and "-off" or ""), gap = 6, align = "center" }, {
          ui.column({ flexGrow = 1 }, { row }),
          ui.button({
            key = "eye-" .. item.key .. (isOff and "-off" or ""),
            glyph = isOff and "eye-off" or "eye",
            variant = "ghost",
            controlSize = "sm",
            tooltip = isOff and tr("panel.show-row") or tr("panel.hide-row"),
            onClick = function()
              hidden[group] = (not isOff) or nil
              saveHidden()
              render()
            end,
          }),
        })
      end
      table.insert(rows, row)
    end
    if #rows > 0 then
      table.insert(children, heading(section.title))
      table.insert(children, card(rows))
    end
  end
  return children
end

local function subtitle()
  if data == nil then
    return tr("panel.not-connected")
  end
  local parts = { data.link == "wireless" and tr("panel.wireless") or tr("panel.cable") }
  if data.battery ~= nil then
    local b = tr("ui.level", { level = data.battery.level })
    if data.battery.charging then
      b = b .. " · " .. tr("ui.charging")
    end
    table.insert(parts, b)
  end
  return table.concat(parts, " · ")
end

render = function()
  local header = {
    ui.glyph({ name = "mouse", size = 22, color = "primary" }),
    ui.column({ gap = 2, flexGrow = 1 }, {
      ui.label({
        text = data and data.name or tr("panel.title"),
        fontSize = 16,
        fontWeight = "bold",
        maxLines = 1,
      }),
      ui.label({ text = subtitle(), fontSize = 11, color = "on_surface_variant" }),
    }),
    ui.button({
      key = "reload",
      glyph = "refresh",
      variant = "ghost",
      tooltip = tr("panel.reload"),
      enabled = not busy,
      onClick = function()
        run("state")
      end,
    }),
    ui.button({
      key = editing and "edit-on" or "edit",
      glyph = editing and "check" or "eye",
      text = editing and tr("panel.edit-done") or nil,
      variant = editing and "primary" or "ghost",
      tooltip = editing and tr("panel.edit-done") or tr("panel.edit"),
      onClick = function()
        editing = not editing
        render()
      end,
    }),
    ui.button({
      key = "open-app",
      glyph = "external-link",
      variant = "ghost",
      tooltip = tr("panel.open-app"),
      onClick = function()
        noctalia.runAsync({ "python3", "-I", script(), "app" })
        panel.close()
      end,
    }),
  }
  if closeButton then
    table.insert(header, ui.button({
      key = "close",
      glyph = "close",
      variant = "ghost",
      onClick = function()
        panel.close()
      end,
    }))
  end

  local children = { ui.row({ gap = 10, align = "center" }, header) }
  if status ~= nil then
    table.insert(children, ui.label({ text = status.text, color = status.color, fontSize = 12 }))
  end
  table.insert(children, ui.scroll({ flexGrow = 1, gap = 10 }, body()))
  panel.render(ui.column({ flexGrow = 1, gap = 12, align = "stretch" }, children))
end

function onOpen(_context)
  closeButton = noctalia.getSetting("shell.show_close_buttons") ~= false
  radiusScale = noctalia.getSetting("shell.corner_radius_scale") or 1.0
  editing = false
  loadHidden()
  render() -- the last reading, while a fresh one loads
  if #queue == 0 then
    run("state")
  end
end
