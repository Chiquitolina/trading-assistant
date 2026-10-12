// PM2 configuration for Reaction Lab. Never starts exchange/trading processes.
module.exports = {
  apps: [
    {
      name: 'reaction-lab-scanner',
      cwd: '/home/multiservices-1m',
      script: '/usr/bin/python3',
      args: '-u /home/multiservices-1m/reaction_lab_continuous_scanner.py --project /home/multiservices-1m --loop --poll-seconds 60',
      autorestart: true,
      restart_delay: 10000,
      min_uptime: '30s',
      max_restarts: 10,
      time: true,
    },
    {
      name: 'reaction-lab-auto-paths',
      cwd: '/home/multiservices-1m',
      script: '/usr/bin/python3',
      args: '-u /home/multiservices-1m/reaction_lab_auto_paths.py --project /home/multiservices-1m --loop --poll-seconds 60',
      autorestart: true,
      restart_delay: 10000,
      min_uptime: '30s',
      max_restarts: 10,
      time: true,
    },
  ],
};
