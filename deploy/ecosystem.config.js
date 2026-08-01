module.exports = {
  apps: [
    {
      name: "trading-agent",
      cwd: "/opt/trading-agent",
      script: "main.py",
      interpreter: "python3",
      autorestart: true,
      max_restarts: 20,
      restart_delay: 15000,
      watch: false,
      max_memory_restart: "512M",
      out_file: "/var/log/trading-agent/out.log",
      error_file: "/var/log/trading-agent/err.log",
      time: true,
    },
  ],
};
