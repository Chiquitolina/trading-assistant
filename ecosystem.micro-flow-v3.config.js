
// Independent PM2 process: no edits to LIVE, V1/V2, shards or WSClient.
module.exports = {
  apps: [
    {
      name: 'micro-flow-v3-ethusdt',
      cwd: '/home/multiservices-1m',
      script: 'scripts/run_micro_flow_v3_ethusdt.sh',
      interpreter: '/bin/bash',
      exec_mode: 'fork',
      instances: 1,
      autorestart: true,
      restart_delay: 10000,
      min_uptime: '60s',
      max_restarts: 5,
      kill_timeout: 30000,
      time: true,
      out_file: 'reports/micro_flow_v3/logs/pm2-v3-out.log',
      error_file: 'reports/micro_flow_v3/logs/pm2-v3-error.log',
      env: { PYTHONUNBUFFERED: '1' },
    },
  ],
};
