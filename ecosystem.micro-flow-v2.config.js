module.exports = {
  apps: [
    {
      name: "micro-flow-v2",
      cwd: "/home/multiservices-1m",
      script: "/usr/bin/python3",
      args: "-u -m engine.live.research.micro_flow_v2_episode_collector",
      interpreter: "none",
      autorestart: true,
      watch: false,
      max_restarts: 20,
      restart_delay: 5000,
      env: {
        PYTHONUNBUFFERED: "1"
      }
    }
  ]
};
