# The README's demo GIF: vhs docs/demo/demo.tape (charmbracelet/vhs). Replays a recorded real run (replay.sh).
Output docs/demo/demo.gif
Set Shell "bash"
Set FontSize 14
Set Width 1680
Set Height 330
Set Padding 18
Set Theme "Dracula"
Set TypingSpeed 35ms
Hide
Type "export PS1='$ ' && clear" Enter
Show
Type "# an SLO is only trusted once its alert has been seen firing:" Enter
Sleep 400ms
Type "# kill every sample-app pod; the prediction: the probe SLO pages within 10 min" Enter
Sleep 400ms
Type "scripts/experiment.py apps/sample-app/experiments/sample-app-down.yaml" Sleep 300ms
Hide
Type " >/dev/null 2>&1; clear; printf '$ scripts/experiment.py apps/sample-app/experiments/sample-app-down.yaml\n'; docs/demo/replay.sh" Enter
Show
Sleep 17s
Sleep 4s
