module.exports = { apps: [{
  name: 'reaction-lab-market-telemetry',
  script: '/usr/bin/python3', interpreter: 'none',
  args: '-u /home/multiservices-1m/reaction_lab_market_telemetry.py --project /home/multiservices-1m --loop --poll-seconds 300 --max-symbols 290',
  cwd: '/home/multiservices-1m', autorestart: true,
  max_restarts: 10, restart_delay: 10000
}]};
