// RESEARCH-ONLY, opt-in after pilot. <=80 REST calls/cycle, >=50% rotation.
module.exports = { apps: [{
  name: 'reaction-lab-market-telemetry',
  script: '/usr/bin/python3', interpreter: 'none',
  args: '-u /home/multiservices-1m/reaction_lab_market_telemetry.py --project /home/multiservices-1m --loop --poll-seconds 45 --max-symbols 20 --priority-enabled --priority-scan-symbols 60 --max-priority 10',
  cwd: '/home/multiservices-1m', autorestart: true,
  max_restarts: 10, restart_delay: 15000
}]};
