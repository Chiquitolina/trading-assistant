// New context-enrichment worker ONLY. Do not restart existing scanner/path workers.
module.exports = {
  apps: [{
    name: 'reaction-lab-context',
    cwd: '/home/multiservices-1m',
    script: '/usr/bin/python3',
    args: '-u /home/multiservices-1m/reaction_lab_context.py --project /home/multiservices-1m --loop --poll-seconds 60 --limit 50',
    autorestart: true,
    restart_delay: 15000,
    min_uptime: '30s',
    max_restarts: 10,
    time: true,
  }],
};
