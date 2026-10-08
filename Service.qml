import QtQuick
import Quickshell
import Quickshell.Io

// Headless collector for the stock omarchy.agents panel.
// Writes ~/.local/state/omarchy/agents/usage/antigravity.json; the built-in
// AI button already watches that directory and draws whatever appears.
Item {
  id: root

  property var manifest: null
  property var shell: null

  readonly property string home: Quickshell.env("HOME") || ""
  readonly property string stateHome: Quickshell.env("XDG_STATE_HOME") || (home + "/.local/state")
  readonly property string claudeRecord: stateHome + "/omarchy/agents/usage/claude.json"

  readonly property string pluginDir: {
    if (manifest && manifest.__sourceDir) return String(manifest.__sourceDir)
    var url = String(Qt.resolvedUrl("."))
    var path = decodeURIComponent(url.replace(/^file:\/\//, "")).replace(/\/$/, "")
    if (path !== "" && path !== ".") return path
    var id = manifest && manifest.id ? manifest.id : "io.github.gokivego.antigravity-usage"
    return home + "/.config/omarchy/plugins/" + id
  }

  readonly property string collector: {
    var url = String(Qt.resolvedUrl("collect.py"))
    var path = decodeURIComponent(url.replace(/^file:\/\//, ""))
    if (path !== "" && path !== "collect.py") return path
    return pluginDir + "/collect.py"
  }

  function collect(force) {
    if (collector === "" || collectProcess.running) return
    var cmd = ["python3", collector, "--write"]
    if (force === true) cmd.push("--force")
    collectProcess.command = cmd
    collectProcess.running = true
  }

  function clearRecord() {
    if (collector === "") return
    clearProcess.command = ["python3", collector, "--clear"]
    clearProcess.running = true
  }

  Timer {
    interval: 300000
    running: true
    repeat: true
    triggeredOnStart: true
    onTriggered: root.collect(false)
  }

  IpcHandler {
    target: "io.github.gokivego.antigravity-usage"
    function refresh(): string {
      root.collect(true)
      return "ok"
    }
  }

  // Stock panel refresh rewrites claude.json. Use that as a cue so Antigravity
  // updates when the user hits r, not only on this timer.
  FileView {
    path: root.claudeRecord
    watchChanges: true
    printErrors: false
    onFileChanged: root.collect(false)
  }

  Process {
    id: collectProcess
    running: false
    stderr: StdioCollector {
      waitForEnd: true
      onStreamFinished: if (text.trim() !== "") console.warn("antigravity-usage", text.trim())
    }
  }

  Process {
    id: clearProcess
    running: false
  }
}
