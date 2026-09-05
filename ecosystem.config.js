module.exports = {
  apps: [
    {
      name: "trading-agent",
      cwd: "/home/ubuntu/trading-agent",
      script: "main.py",
      interpreter: "/home/ubuntu/trading-agent/.venv/bin/python",
      autorestart: true,
      max_restarts: 20,
      restart_delay: 15000,
      watch: false,
      max_memory_restart: "512M",
      out_file: "/home/ubuntu/trading-agent/logs/pm2-out.log",
      error_file: "/home/ubuntu/trading-agent/logs/pm2-error.log",
      time: true,
    },
  ],
};
