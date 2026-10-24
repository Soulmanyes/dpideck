--!nonstrict
-- DPIdeck bar widget: mouse glyph plus percentage.
--
-- battery.py does the HID read with DPIdeck's atk.py and prints JSON. It
-- never wakes the mouse, so a sleeping mouse reports {"asleep": true}; the
-- widget then keeps the last level and dims the glyph.

local LOW = 20
local POLL_MS = 60000

local level = nil
local charging = false
local asleep = false
local errorText = nil
local busy = false

local function render()
  if errorText ~= nil and level == nil then
    barWidget.setGlyph("mouse-off")
    barWidget.setGlyphColor("on_surface_variant")
    barWidget.setText("")
    barWidget.setTooltip(errorText)
    return
  end
  if level == nil then
    barWidget.setGlyph("mouse")
    barWidget.setGlyphColor("on_surface_variant")
    barWidget.setText("")
    barWidget.setTooltip(noctalia.tr("ui.asleep"))
    return
  end

  local color = "on_surface"
  if asleep then
    color = "on_surface_variant"
  elseif charging then
    color = "secondary"
  elseif level <= LOW then
    color = "error"
  end
  barWidget.setGlyph("mouse")
  barWidget.setGlyphColor(color)
  barWidget.setText(tostring(level) .. "%")

  local tip = noctalia.tr("ui.level", { level = level })
  if charging then
    tip = tip .. " · " .. noctalia.tr("ui.charging")
  end
  if asleep then
    tip = tip .. " · " .. noctalia.tr("ui.asleep-last")
  end
  barWidget.setTooltip(tip)
end

local function poll()
  if busy then
    return
  end
  busy = true
  local script = noctalia.pluginDir() .. "/battery.py"
  noctalia.runAsync("python3 -I '" .. script .. "'", function(result)
    busy = false
    if type(result) ~= "table" or result.timedOut or result.exitCode ~= 0 then
      return -- keep showing the last reading
    end
    local data = noctalia.json.decode(result.stdout or "")
    if type(data) ~= "table" then
      return
    end
    if data.level ~= nil then
      level = data.level
      charging = data.charging == true
      asleep = false
      errorText = nil
    elseif data.asleep then
      asleep = true
    else
      -- no dongle or no permission: forget the old level
      level = nil
      errorText = tostring(data.error or noctalia.tr("ui.no-mouse"))
    end
    render()
  end, 30000)
end

function update()
  noctalia.setUpdateInterval(POLL_MS)
  render()
  poll()
end
