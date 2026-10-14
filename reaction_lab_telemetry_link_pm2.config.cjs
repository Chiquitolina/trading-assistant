// Optional research sidecar; start only after isolated pilot.
module.exports={apps:[{
 name:'reaction-lab-telemetry-link',script:'/usr/bin/python3',interpreter:'none',
 args:'-u /home/multiservices-1m/reaction_lab_telemetry_link.py --project /home/multiservices-1m --loop --poll-seconds 60 --limit 100',
 cwd:'/home/multiservices-1m',autorestart:true,max_restarts:10,restart_delay:10000
}]};
