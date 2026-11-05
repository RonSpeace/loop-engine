【进程安全红线】你的 ACP 驱动进程 PID={pid}（进程组）。
禁止 kill/killpg/pkill 该 PID、其进程组及任何 kimi 进程（含 kimi-chat-acp）。
清理残留进程必须使用项目约定的实例脚本或精确 PID，禁止 pkill -f kimi 类路径猜测（会误杀他人会话与交互式 kimi）。
禁止自行编写 trap 清理命令；禁止 kill/killpg/pkill 清理 ACP 驱动进程或其进程组。