// Octopilot's launcher: a command-palette entry that opens the Octopilot
// space. It has no view; mod.json's palette carries the command, which opens
// $OCTOPILOT_DIR (default ~/src/octopilot): the checkout installed on this machine.

import { View, div } from "gpui-kit";

export default class OctopilotLauncher extends View {
  render(cx) {
    return div();
  }
}
