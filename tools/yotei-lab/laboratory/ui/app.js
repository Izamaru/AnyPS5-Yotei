'use strict';
const $ = id => document.getElementById(id);
const token = document.querySelector('meta[name="anyps5-session"]').content;
const format = (v,d=2) => new Intl.NumberFormat('pt-BR',{maximumFractionDigits:d,minimumFractionDigits:d}).format(v);
const escape = s => String(s??'').replace(/[&<>"']/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
const cacheText = v => v>=1024 ? `${new Intl.NumberFormat('pt-BR',{maximumFractionDigits:2}).format(v/1024)} GiB` : `${v} MiB`;
const labels={inicio:'Visão geral',ajustes:'Ajustes',manutencao:'Manutenção',backups:'Backups',atividade:'Atividade'};
const actions={run:'Teste do jogo',status:'Diagnóstico do ambiente',audit:'Auditoria do runtime','check-updates':'Consulta de atualizações',setup:'Preparação de ferramentas',build:'Compilação e testes','build-relinker':'Compilação do conversor',relink:'Conversão do executável','apply-lab-update':'Correções locais',rollback:'Restauração da versão anterior',backup:'Backup completo'};
let cacheStages=[1024,2048,3072,4096];
let settings=null, snapshot=null, dirty=false, saveTimer=null, saveGeneration=0, saveQueue=Promise.resolve(), logCursor=0, logLines=[], lastJobKey='', currentPage='inicio', toastTimer=null, closed=false;
async function api(path,body){
 const options=body===undefined?{}:{method:'POST',headers:{'Content-Type':'application/json','X-AnyPS5-Token':token},body:JSON.stringify(body)};
 const response=await fetch('/api/'+path,options);const value=await response.json();
 if(!response.ok)throw new Error(value.error||'Não foi possível concluir a solicitação.');return value;
}
function toast(message){$('toast').textContent=message;$('toast').classList.remove('hidden');clearTimeout(toastTimer);toastTimer=setTimeout(()=>$('toast').classList.add('hidden'),5000);}
function navigate(page){
 if(!labels[page])page='inicio';currentPage=page;
 document.querySelectorAll('.page').forEach(el=>el.classList.toggle('active',el.id==='page-'+page));
 document.querySelectorAll('[data-page]').forEach(el=>{el.classList.toggle('active',el.dataset.page===page);if(el.dataset.page===page)el.setAttribute('aria-current','page');else el.removeAttribute('aria-current');});
 history.replaceState(null,'','#'+page);window.scrollTo(0,0);
 if(page==='backups')loadBackups();if(page==='atividade')loadLogs();
}
document.querySelectorAll('[data-page],[data-go]').forEach(button=>button.addEventListener('click',()=>navigate(button.dataset.page||button.dataset.go)));
window.addEventListener('hashchange',()=>navigate(location.hash.slice(1)));
function renderSettings(){
 if(!settings)return;
 const stage=cacheStages.indexOf(settings.cache_mib)+1;
 $('cache-slider').max=cacheStages.length;
 $('cache-slider').value=stage;$('cache-output').textContent=`Estágio ${stage} · ${cacheText(settings.cache_mib)}`;
 $('cache-slider').setAttribute('aria-valuetext',`Estágio ${stage}: ${cacheText(settings.cache_mib)}`);
 $('cache-stage-labels').replaceChildren(...cacheStages.map((amount,index)=>{const mark=document.createElement('span');mark.textContent=index+1;mark.title=`Estágio ${index+1}: ${cacheText(amount)}`;return mark;}));
 $('jobs-slider').value=settings.jobs;$('jobs-output').textContent=settings.jobs;$('capture-toggle').checked=settings.dump_shaders;
 for(const id of ['cache-slider','jobs-slider']){const el=$(id);el.style.setProperty('--fill',`${100*(el.value-el.min)/(el.max-el.min)}%`);}
 document.querySelectorAll('[data-profile]').forEach(el=>{const on=el.dataset.profile===settings.profile;el.classList.toggle('selected',on);el.setAttribute('aria-pressed',on);});
 document.querySelectorAll('[data-cpu]').forEach(el=>{const on=el.dataset.cpu===settings.cpu;el.classList.toggle('selected',on);el.setAttribute('aria-pressed',on);});
 $('home-profile').textContent=settings.profile==='desempenho'?'Desempenho':'Econômico';$('home-cache').textContent=cacheText(settings.cache_mib);
 $('home-cpu').textContent=snapshot?.cpu==='amd'?'AMD':'Intel';
}
function changed(){dirty=true;saveGeneration++;$('save-status').textContent='Salvando…';renderSettings();clearTimeout(saveTimer);saveTimer=setTimeout(()=>save().catch(()=>{}),350);}
async function save(){
 clearTimeout(saveTimer);if(!dirty||!settings)return;
 const generation=saveGeneration, value={...settings};
 const pending=saveQueue.catch(()=>{}).then(()=>api('settings',value));saveQueue=pending;
 try{await pending;if(generation===saveGeneration){dirty=false;$('save-status').textContent='Salvo neste PC';}}
 catch(error){$('save-status').textContent='Não foi salvo';toast(error.message);throw error;}
}
document.querySelectorAll('[data-profile]').forEach(el=>el.addEventListener('click',()=>{settings.profile=el.dataset.profile;settings.cache_mib=settings.profile==='desempenho'?2048:1024;changed();}));
document.querySelectorAll('[data-cpu]').forEach(el=>el.addEventListener('click',()=>{settings.cpu=el.dataset.cpu;changed();}));
$('cache-slider').addEventListener('input',e=>{settings.cache_mib=cacheStages[Number(e.target.value)-1];changed();});
$('jobs-slider').addEventListener('input',e=>{settings.jobs=Number(e.target.value);changed();});
$('capture-toggle').addEventListener('change',e=>{settings.dump_shaders=e.target.checked;changed();});
async function startAction(action){
 try{await save();await api('action',{action});logCursor=0;logLines=[];await refresh();toast('Tarefa iniciada: '+(actions[action]||'operação')+'.');}
 catch(error){toast(error.message);}
}
document.querySelectorAll('[data-action]').forEach(el=>el.addEventListener('click',()=>startAction(el.dataset.action)));
$('stop-test').addEventListener('click',async()=>{try{await api('stop',{});await refresh();}catch(error){toast(error.message);}});
$('copy-log').addEventListener('click',async()=>{try{await navigator.clipboard.writeText(logLines.join('\n'));toast('Mensagens copiadas.');}catch{toast('Selecione e copie o texto da atividade.');}});
$('shutdown').addEventListener('click',async()=>{try{await api('shutdown',{});closed=true;$('connection-error').textContent='Painel encerrado. Você pode fechar esta aba e reabrir pelo INICIAR_AQUI.bat.';$('connection-error').classList.remove('hidden');document.querySelectorAll('button').forEach(el=>el.disabled=true);}catch(error){toast(error.message);}});
function renderState(value){
 snapshot=value;if(Array.isArray(value.cache_stages_mib))cacheStages=value.cache_stages_mib;if(!settings||!dirty)settings={...value.settings};renderSettings();
 $('release').textContent=value.release;$('lab-version').textContent=value.lab_version;$('revision').textContent='Base '+value.revision+' · Windows x64';
 $('runtime-ready').textContent=value.ready?'Runtime preparado':'Preparação necessária';
 const running=value.job?.status==='running';document.body.classList.toggle('busy',running);
 document.querySelectorAll('[data-action]').forEach(el=>{el.disabled=running||(el.dataset.action==='run'&&!value.ready)||(el.dataset.action==='rollback'&&!value.can_rollback);});
 document.querySelectorAll('[data-profile],[data-cpu],input').forEach(el=>el.disabled=running);
 $('shutdown').disabled=running;
 const live=running&&value.job.action==='run';const fps=live?value.fps:value.benchmark_fps;
 $('fps-label').textContent=live?'Apresentações no teste':'Último benchmark';$('fps-value').textContent=fps==null?'—':format(fps);
 $('fps-note').textContent=live?'Estimativa pelo contador de apresentações':'Teste de 7 minutos · cache de 2 GiB';
 const resources=value.resources;document.querySelectorAll('.runtime-metric').forEach(el=>el.classList.toggle('hidden',!resources));$('ram-value').textContent=resources?.process_working_set_bytes?format(resources.process_working_set_bytes/2**30):'—';
 const devices=resources?.gpu?.devices;const used=devices?.[0]?.device_used_bytes;$('vram-value').textContent=used==null?'—':format(used/2**30);
 if(value.job){
  $('job-bar').classList.toggle('hidden',!running&&value.job.status!=='failed');$('job-title').textContent=actions[value.job.action]||value.job.action;
  $('job-message').textContent=value.job.message.replace(/^\[YOTEI\]\s*/,'');
  $('job-indicator').className=running?'spinner':(value.job.status==='failed'?'spinner failed':'spinner done');
  $('stop-test').classList.toggle('hidden',!live);$('log-title').textContent=(actions[value.job.action]||'Operação')+' · '+({running:'Em andamento',completed:'Concluída',failed:'Erro',stopped:'Encerrada'}[value.job.status]);
  $('log-path').textContent=value.job.log;
  const key=value.job.started+':'+value.job.status;
  if(lastJobKey!==key){if(currentPage==='backups')loadBackups();if(lastJobKey&&value.job.status!=='running')toast(value.job.status==='completed'?'Tarefa concluída.':value.job.message);lastJobKey=key;}
 }
}
async function loadBackups(){
 try{
  const data=await api('backups');const rows=data.backups;$('backup-count').textContent=`${rows.length} de ${data.keep} cópias guardadas · ${format(rows.reduce((n,row)=>n+row.bytes,0)/2**30,2)} GiB`;
  const reasons={'before-rollback':'Segurança antes de restaurar','manual-interface':'Criado manualmente','Before FPS/cache investigation on v0.1.2 + local corrections':'Antes dos ajustes de FPS'};
  $('backup-rows').innerHTML=rows.map(row=>{
   const date=row.id.slice(6,8)+'/'+row.id.slice(4,6)+'/'+row.id.slice(0,4)+' · '+row.id.slice(9,11)+':'+row.id.slice(11,13);
   let reason=reasons[row.reason]||row.reason;if(reason.length>58)reason=reason.slice(0,55)+'…';
   return `<tr><td>${escape(date)}<small>${escape(row.id)}</small></td><td>${escape(row.lab_version?'PC Lab '+row.lab_version:'Revisão local')}<small>${escape((row.revision||'').slice(0,12))}</small></td><td title="${escape(row.reason)}">${escape(reason)}</td><td>${format(row.bytes/2**20,0)} MiB</td><td><span class="table-tag ${row.next_rollback?'':'neutral'}">${row.next_rollback?'Próximo retorno':row.rollback_target?'Na cadeia de retorno':'Cópia de segurança'}</span></td></tr>`;
  }).join('');$('backup-empty').classList.toggle('hidden',rows.length>0);
  const target=rows.find(row=>row.next_rollback);$('rollback-description').textContent=target?`Retorno disponível: ${target.id}${target.lab_version?' · PC Lab '+target.lab_version:''}.`:'Não há retorno disponível na cadeia atual. As cópias de segurança permanecem listadas acima.';
 }catch(error){toast(error.message);}
}
async function loadLogs(){
 try{const data=await api('log?after='+logCursor);if(!data.lines.length)return;logCursor=data.lines.at(-1).id;logLines.push(...data.lines.map(row=>row.text));logLines=logLines.slice(-400);const output=$('log-output');const atBottom=output.scrollHeight-output.scrollTop-output.clientHeight<50;output.textContent=logLines.join('\n');if(atBottom)output.scrollTop=output.scrollHeight;}catch{}
}
let refreshing=false;
async function refresh(){if(closed||refreshing)return;refreshing=true;try{const state=await api('state');renderState(state);$('connection-error').classList.add('hidden');await loadLogs();}catch(error){$('connection-error').textContent='O painel não está respondendo. Reabra pelo INICIAR_AQUI.bat se necessário.';$('connection-error').classList.remove('hidden');}finally{refreshing=false;}}
navigate(location.hash.slice(1)||'inicio');refresh();setInterval(refresh,2500);
