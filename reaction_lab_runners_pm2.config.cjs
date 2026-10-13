module.exports = {
  apps: [{
    name: 'reaction-lab-runners',
    script: '/usr/bin/python3',
    interpreter: 'none',
    cwd: '/home/multiservices-1m',
    args: '-u /home/multiservices-1m/reaction_lab_runners.py --project /home/multiservices-1m --loop --poll-seconds 60',
    autorestart: true,
    watch: false,
    max_restarts: 10,
    min_uptime: '10s',
    time: true
  }]
};
