module.exports = {
  apps: [{
    name: 'reaction-lab-btc-context',
    cwd: '/home/multiservices-1m',
    script: '/usr/bin/python3',
    interpreter: 'none',
    args: '-u /home/multiservices-1m/reaction_lab_btc_context.py --project /home/multiservices-1m --loop --poll-seconds 60 --limit 50',
    autorestart: true,
    max_restarts: 10,
    min_uptime: '15s',
    time: true
  }]
};
