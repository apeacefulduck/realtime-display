const fs=require('fs'),path=require('path'),vm=require('vm'),assert=require('assert');
const root=path.join(__dirname,'..');
const rules=JSON.parse(fs.readFileSync(path.join(root,'api/indoor_rules.json'),'utf8'));
const src=fs.readFileSync(path.join(root,'frontend/script.js'),'utf8');
const code=src.slice(src.indexOf('function renderIndoor()'),src.indexOf('function initializeIndoorHelp()'));
let now=1000000;const elements={};
const ctx={indoorRules:rules,indoorState:{sensorAvailable:true,temperature:23.4,humidity:47,lastUpdated:new Date(now).toISOString(),temperatureStatus:'Konforlu',humidityStatus:'Konforlu'},
  indoorReceivedAt:now,Date:Object.assign(function(){}, {parse:Date.parse,now:()=>now}),Number,
  ui:id=>(elements[id]??={}),relativeTime:()=>''};
vm.createContext(ctx);vm.runInContext(code,ctx);
for(const age of [0,12000,60000,120000,300000,359999]) {
  now=1000000+age;ctx.renderIndoor();assert.equal(elements.indoorTemperature.textContent,'23.4 °C');
}
now=1360000;ctx.renderIndoor();assert.equal(elements.indoorTemperature.textContent,'--');
ctx.indoorState.lastUpdated=new Date(now).toISOString();ctx.indoorReceivedAt=now;ctx.renderIndoor();assert.equal(elements.indoorTemperature.textContent,'23.4 °C');
ctx.indoorState.sensorAvailable=false;ctx.renderIndoor();assert.equal(elements.indoorTemperature.textContent,'--');
const generated=fs.readFileSync(path.join(root,'frontend/indoor-rules.js'),'utf8');
assert.deepEqual(JSON.parse(generated.slice(generated.indexOf('=')+1).trim().replace(/;$/,'')),rules);
new vm.Script(src);
console.log('PASS: actual frontend indoor renderer: fresh through 300s, stale at 360s, heartbeat recovery, failed sample, generated rules match, JS syntax');
