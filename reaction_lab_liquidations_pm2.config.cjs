module.exports = { apps: [{
 name: 'reaction-lab-liquidations',
 script: '/usr/bin/python3', interpreter: 'none',
 args: '-u /home/multiservices-1m/reaction_lab_liquidations.py --project /home/multiservices-1m --loop',
 cwd: '/home/multiservices-1m', autorestart: true,
 max_restarts: 10, restart_delay: 15000
}]};
