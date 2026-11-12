[Process-safety red line] Your ACP driver process PID={pid} (process group).
Do not kill/killpg/pkill this PID, its process group, or any kimi process (including kimi-chat-acp).
To clean up leftover processes, use the project's instance script or an exact PID; never guess by path with pkill -f kimi and the like (it kills other people's sessions and interactive kimi).
Do not write your own trap cleanup commands; do not kill/killpg/pkill the ACP driver process or its process group to clean up.