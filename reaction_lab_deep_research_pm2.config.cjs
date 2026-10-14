module.exports = { apps: [{
  name: 'reaction-lab-deep-research',
  script: '/usr/bin/python3', interpreter: 'none',
  args: '-u /home/multiservices-1m/reaction_lab_deep_collector.py --project /home/multiservices-1m --loop --poll-seconds 60 --limit 100',
  cwd: '/home/multiservices-1m', autorestart: true,
  max_restarts: 10, restart_delay: 5000
}]};
