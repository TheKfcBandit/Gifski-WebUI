'use strict';
(() => {
  const $ = id => document.getElementById(id);
  const $$ = selector => [...document.querySelectorAll(selector)];
  const clone = value => JSON.parse(JSON.stringify(value));
  const defaults = {
    source:{kind:'video',path:'',paths:[]}, trim:{start:0,end:null}, fps:20, speed:1,
    width:640,height:null,quality:90,lossyQuality:null,motionQuality:null,effort:'normal',
    repeat:0,bounce:false,noSort:false,quiet:false,matte:null,fixedColors:[],y4mColorOverride:null,
    outputName:'animation.gif',crop:{enabled:false,x:0,y:0,width:640,height:360},scaleFilter:'lanczos',
    denoise:0,sharpen:0,reverse:false,customFilter:'',pipeline:'auto',
    smart:{targetMB:20,effort:'balanced'},performance:{threads:0,hardwareDecode:'none'}
  };
  const builtinPresets = [
    {id:'balanced',name:'Balanced',icon:'◈',description:'A versatile balance of detail, motion, and size.',settings:{width:640,fps:20,quality:90}},
    {id:'small',name:'Compact',icon:'◧',description:'Smaller dimensions and fewer frames, with room to tune quality upward.',settings:{width:480,fps:15,quality:75}},
    {id:'quality',name:'Fidelity',icon:'✳',description:'High detail and extra effort. Expect large files; compare with normal effort.',settings:{width:960,fps:25,quality:100,effort:'extra'}},
    {id:'ui',name:'UI & text',icon:'⌘',description:'Crisp interface details with a moderate frame rate. Inspect small text closely.',settings:{width:800,fps:15,quality:95,effort:'extra'}},
    {id:'motion',name:'Motion',icon:'≋',description:'More frames for fast action. Check GIF timing and the measured size.',settings:{width:640,fps:30,quality:90,motionQuality:95}},
    {id:'alpha',name:'Transparent',icon:'▦',description:'PNG preparation preserves alpha. GIF can only keep fully opaque or transparent pixels.',settings:{width:640,fps:20,quality:95,pipeline:'png'}}
  ];
  const modes = [
    {id:'default',name:'Default',icon:'◈',description:'A balanced recipe for everyday GIFs. Trim your clip and create.'},
    {id:'smart',name:'Smart',icon:'✦',description:'Find a detailed, smooth GIF within your own file-size limit.'},
    {id:'discord',name:'Discord',icon:'◎',description:'The same intelligent search, with a fixed 20 MB limit.'},
    {id:'presets',name:'Other presets',icon:'▦',description:'Choose a starting point or one of your saved recipes.'},
    {id:'custom',name:'Custom',icon:'◧',description:'Tune dimensions, frame rate, and quality yourself.'},
    {id:'advanced',name:'Advanced',icon:'⚙',description:'Full control of Gifski and FFmpeg preprocessing, grouped by purpose.'}
  ];
  let settings=clone(defaults), toolsState=null, metadata=null, jobs=[], currentJob=null;
  let selectedMode='default', selectedPreset='balanced', pollBusy=false, submitting=false, uploadBusy=false, sourceLoading=false;
  let noticeTimer=null, planTimer=null, recipeRevision=0;
  let sourceRequest=0, planRequest=0, statusRequest=0, pollTimer=null, versionRequestPending=false, versionSignature='', jobSelectionRequest=0;
  let latestEstimateJob=null, estimatePendingId=null, wakeLock=null, wakeRequestPending=false, wakeDenied=false;
  const jobRevisions=new Map();
  const sourceMatches=new Map(), toolDrafts=new Map();
  let dependencyState=null, dependencyPoll=null, dependencyBusy=false, setupChecked=false;
  let customPresets=[];
  try { const stored=JSON.parse(localStorage.getItem('gifski-workbench-presets')||'[]');
    if(Array.isArray(stored))customPresets=stored.slice(0,100).flatMap(item=>{try{return [validateImported(item)];}catch{return [];}});
  } catch { customPresets=[]; }
  function presetId(){return 'custom-'+(globalThis.crypto?.randomUUID?.()||Date.now().toString(36)+'-'+Math.random().toString(36).slice(2));}
  const adaptive=()=>['smart','discord'].includes(selectedMode);
  const get=(obj,key)=>key.split('.').reduce((a,k)=>a?.[k],obj);
  const set=(obj,key,value)=>{const path=key.split('.');const last=path.pop();let t=obj;for(const k of path)t=t[k];t[last]=value;};
  function el(tag,className,text){const node=document.createElement(tag);if(className)node.className=className;if(text!==undefined)node.textContent=String(text);return node;}
  function button(text,className,fn){const node=el('button',className,text);node.type='button';node.addEventListener('click',fn);return node;}
  function notice(message,error=false){$('noticeText').textContent=message;$('notice').classList.toggle('error',error);$('notice').hidden=false;clearTimeout(noticeTimer);noticeTimer=setTimeout(()=>{$('notice').hidden=true;},error?14000:5500);}
  async function api(path,body,opts={}){
    const {timeoutMs=65000,...options}=opts,controller=new AbortController();
    const timer=setTimeout(()=>controller.abort(),timeoutMs);
    try{
      const response=await fetch(path,{credentials:'same-origin',...(body===undefined?{}:{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify(body)}),...options,signal:controller.signal});
      const type=response.headers.get('content-type')||'',data=type.includes('json')?await response.json():{error:await response.text()};
      if(!response.ok)throw new Error(data.error||data.message||`Request failed (${response.status})`);return data;
    }catch(error){if(error.name==='AbortError')throw new Error('The local server took too long to respond. Keep the launcher window open; a running job may still be working.');throw error;}
    finally{clearTimeout(timer);}
  }
  const safeRun=fn=>(...args)=>Promise.resolve().then(()=>fn(...args)).catch(error=>notice(error.message||String(error),true));
  function bytes(value){if(value==null||value===''||!Number.isFinite(Number(value)))return '—';const n=Number(value);return n>=1048576?`${(n/1048576).toFixed(2)} MiB`:n>=1024?`${(n/1024).toFixed(1)} KiB`:`${n} B`;}
  function seconds(value){if(value==null||value==='')return '—';const n=Number(value);if(!Number.isFinite(n)||n<0)return '—';return n>=60?`${Math.floor(n/60)}:${String(Math.floor(n%60)).padStart(2,'0')}`:`${n.toFixed(2)} s`;}
  function sourceExists(){return !!(settings.source.path||settings.source.paths?.length);}
  function syncFields(){
    $$('[data-key]').forEach(node=>{const value=get(settings,node.dataset.key);if(node.type==='checkbox')node.checked=!!value;else node.value=value??'';});
    $('fixedColors').value=(settings.fixedColors||[]).join(', ');
    $('loopMode').value=settings.repeat===-1?'once':settings.repeat===0?'forever':'count';
    $('repeatField').hidden=settings.repeat<=0;$$('[data-key="repeat"]')[0].disabled=settings.repeat<=0;
    $('targetMB').value=selectedMode==='discord'?20:settings.smart.targetMB;
    $('searchEffort').value=settings.smart.effort;$('sequenceRate').value=settings.fps;
    updateRecipe();renderPresets();updateSourceMatches();
  }
  function changed(key){
    recipeRevision++;
    if(key&&!['trim.start','trim.end','speed','repeat','bounce','outputName','smart.targetMB','smart.effort','sequenceRate'].includes(key)&&!adaptive()){
      if(selectedMode!=='advanced')selectedMode='custom';selectedPreset=null;
    }
    updateRecipe();updateSourceMatches();renderPresetSelection();schedulePlan();
  }
  function updateRecipe(){
    const auto=adaptive(), manual=['custom','advanced'].includes(selectedMode);
    renderEstimate();
    $('qualityValue').textContent=settings.quality;
    $('effortHint').textContent={normal:'Gifski’s default mode, without an effort flag. Normal balances encoding time and fidelity.',extra:'Uses Gifski’s --extra flag. Prioritizes fidelity and usually creates larger files.',fast:'Uses Gifski’s --fast flag. Faster, with lower fidelity and typically larger files than normal.'}[settings.effort];
    const dim=settings.width?`${settings.width}${settings.height?' × '+settings.height:''} px`:settings.height?`${settings.height} px high`:'Auto dimensions';
    const recipe=`${dim} · ${settings.fps} fps · Q${settings.quality}`;
    $('recipeSummary').textContent=auto?`${selectedMode==='discord'?'Discord':'Smart'} · ≤ ${selectedMode==='discord'?20:settings.smart.targetMB} MB`:recipe;
    $('presetOverviewRecipe').textContent=recipe;
    $('presetOverviewTitle').textContent=selectedMode==='default'?'A dependable starting point':builtinPresets.find(p=>p.id===selectedPreset)?.name||customPresets.find(p=>p.id===selectedPreset)?.name||'Your selected recipe';
    $('presetOverviewDescription').textContent=selectedMode==='default'?'Default balances detail, smoothness, and file size. Choose Smart when the file has to fit a limit.':builtinPresets.find(p=>p.id===selectedPreset)?.description||'Your saved settings are ready. Customize this recipe to fine-tune it.';
    $('presetHint').textContent=modes.find(m=>m.id===selectedMode)?.description||'';
    const extra=[settings.crop.enabled?'crop':null,settings.reverse?'reverse':null,settings.denoise?'denoise':null,settings.sharpen?'sharpen':null,settings.customFilter?'custom filters':null,settings.fixedColors.length?'fixed colors':null,settings.matte?'matte':null,!['auto','png'].includes(settings.pipeline)?settings.pipeline+' input':null].filter(Boolean);
    $('processingSummary').hidden=!extra.length||selectedMode==='advanced';$('processingSummary').textContent=`Also active: ${extra.join(', ')} · Edit in Advanced`;
    $('otherPresets').hidden=selectedMode!=='presets';$('smartControls').hidden=!auto;
    $('manualControls').hidden=!manual;$('advancedControls').hidden=selectedMode!=='advanced';
    $('presetOverview').hidden=auto||manual;$('presetActions').hidden=!['presets','custom','advanced'].includes(selectedMode);
    $('targetMB').disabled=selectedMode==='discord';
    $('smartHeading').textContent=selectedMode==='discord'?'Discord · 20 MB maximum':'Fit your file-size limit';
    $('sequenceRateField').hidden=settings.source.kind!=='sequence'||manual;
    const rawDuration=metadata?.duration??metadata?.format?.duration;
    const duration=rawDuration!=null?Number(rawDuration):settings.source.kind==='sequence'&&metadata?.frameCount?metadata.frameCount/settings.fps:NaN;
    const start=Number(settings.trim.start)||0,end=settings.trim.end==null?duration:Number(settings.trim.end);
    if(sourceExists()&&Number.isFinite(end)&&end>start){const out=(end-start)/settings.speed*(settings.bounce?2:1);$('durationSummary').textContent=`≈ ${seconds(out)}${!auto?' · '+Math.ceil(out*settings.fps)+' frames':''}${settings.bounce?' · bounce':''}`;}
    else $('durationSummary').textContent=sourceExists()?'Ready to encode':'Load a clip to begin';
    $('estimateJob').hidden=auto;$('estimateJob').disabled=!sourceExists()||submitting||uploadBusy||sourceLoading||!!estimatePendingId;
    $('runJob').disabled=!sourceExists()||submitting||uploadBusy||sourceLoading;
    $('runJob').replaceChildren(document.createTextNode(auto?'Find best fit':'Encode GIF'),el('span','', '↗'));
    const canMark=!!$('sourceVideo').getAttribute('src')&&!$('sourceVideo').hidden;
    $('markStart').disabled=!canMark;$('markEnd').disabled=!canMark;
  }
  function renderPresetSelection(){
    $$('[data-mode]').forEach(b=>{b.classList.toggle('selected',b.dataset.mode===selectedMode);b.setAttribute('aria-pressed',String(b.dataset.mode===selectedMode));});
    $$('[data-preset]').forEach(b=>{b.classList.toggle('selected',b.dataset.preset===selectedPreset);b.setAttribute('aria-pressed',String(b.dataset.preset===selectedPreset));});
  }
  function renderPresets(){
    if(!$('modeSelector').children.length)$('modeSelector').replaceChildren(...modes.map(m=>{const b=button('','preset-card',()=>selectMode(m.id));b.dataset.mode=m.id;b.append(el('span','preset-icon',m.icon),el('span','',m.name));return b;}));
    $('presetGrid').replaceChildren(...builtinPresets.slice(1).map(p=>{const b=button(p.name,'preset-card small',()=>applyPreset(p));b.dataset.preset=p.id;b.title=p.description;return b;}));
    $('customPresetList').replaceChildren(...customPresets.map((p,index)=>{const row=el('div','custom-preset'),choose=button(p.name,'preset-card small',()=>applyPreset(p));choose.dataset.preset=p.id;row.append(choose,button('×','button subtle compact',()=>{customPresets.splice(index,1);persistPresets();renderPresets();}));row.lastChild.setAttribute('aria-label',`Delete preset ${p.name}`);return row;}));
    $('noSavedPresets').hidden=!!customPresets.length;renderPresetSelection();
  }
  function selectMode(mode){
    if(mode==='default'){applyPreset(builtinPresets[0], 'default');return;}
    selectedMode=mode;recipeRevision++;
    if(adaptive()){sourceMatches.clear();settings.width=null;settings.height=null;settings.quality=100;settings.motionQuality=null;settings.lossyQuality=null;settings.effort='normal';if(settings.source.kind!=='sequence')settings.fps=20;}
    syncFields();schedulePlan();
  }
  function applyPreset(p,mode='presets'){
    const source=clone(settings.source),trim=clone(settings.trim),outputName=settings.outputName;
    settings={...clone(defaults),...clone(p.settings),source,trim,outputName};
    for(const k of ['crop','smart','performance'])settings[k]={...defaults[k],...p.settings[k]};
    selectedMode=mode;selectedPreset=p.id||null;sourceMatches.clear();recipeRevision++;syncFields();schedulePlan();
  }
  function persistPresets(){try{localStorage.setItem('gifski-workbench-presets',JSON.stringify(customPresets));}catch{notice('Browser storage is full or unavailable. Export recipes to keep them.',true);}}
  function recipeForSave(){const recipe={};for(const key of Object.keys(defaults))if(!['source','trim'].includes(key))recipe[key]=clone(settings[key]);return recipe;}
  function validateImported(obj){
    if(!obj||typeof obj!=='object'||Array.isArray(obj))throw new Error('This file does not contain a recipe object.');
    const item=Object.prototype.hasOwnProperty.call(obj,'settings')?obj:{name:'Imported recipe',settings:obj};
    if(!item.settings||typeof item.settings!=='object'||Array.isArray(item.settings))throw new Error('Invalid recipe settings.');
    const clean={}, enums={effort:['normal','fast','extra'],pipeline:['auto','png','y4m','direct'],scaleFilter:['lanczos','bicubic','bilinear','neighbor','spline','area'],'smart.effort':['quick','balanced','thorough'],'performance.hardwareDecode':['none','auto'],y4mColorOverride:[null,'','bt709','fcc','bt470bg','bt601','smpte240','ycgco']};
    const ranges={quality:[1,100,true],lossyQuality:[1,100,true,true],motionQuality:[1,100,true,true],width:[1,65535,true,true],height:[1,65535,true,true],fps:[0.001,100],speed:[0.001,1000],repeat:[-1,32767,true],denoise:[0,20],sharpen:[0,5],'crop.x':[0,65535,true],'crop.y':[0,65535,true],'crop.width':[1,65535,true],'crop.height':[1,65535,true],'smart.targetMB':[0.01,100000],'performance.threads':[0,256,true]};
    function check(key,value,base){
      if(ranges[key]){const [min,max,integer,nullable]=ranges[key];if(value===null&&nullable)return value;if(typeof value!=='number'||!Number.isFinite(value)||value<min||value>max||(integer&&!Number.isInteger(value)))throw new Error(`Invalid recipe value for ${key}.`);return value;}
      if(enums[key]){if(!enums[key].includes(value))throw new Error(`Unknown recipe option for ${key}.`);return value||null;}
      if(key==='matte'){if(value!==null&&(typeof value!=='string'||!/^#?[a-f\d]{6}$/i.test(value)))throw new Error('Recipe matte must be null or a six-digit RGB color.');return value;}
      if(key==='fixedColors'){if(!Array.isArray(value)||value.length>255||value.some(c=>typeof c!=='string'||!/^#?[a-f\d]{6}$/i.test(c)))throw new Error('Recipe fixed colors must be an array of at most 255 RGB hex colors.');return value.slice();}
      if(typeof base==='boolean'){if(typeof value!=='boolean')throw new Error(`Recipe ${key} must be true or false.`);return value;}
      if(typeof base==='string'){if(typeof value!=='string'||value.length>(key==='customFilter'?10000:180)||value.includes('\0'))throw new Error(`Invalid recipe text for ${key}.`);return value;}
      throw new Error(`Unsupported recipe value: ${key}`);
    }
    for(const key of Object.keys(defaults)){if(['source','trim'].includes(key)||!Object.prototype.hasOwnProperty.call(item.settings,key))continue;const value=item.settings[key];if(['crop','smart','performance'].includes(key)){if(!value||typeof value!=='object'||Array.isArray(value))throw new Error(`Invalid ${key} settings.`);clean[key]={};for(const subkey of Object.keys(defaults[key]))if(Object.prototype.hasOwnProperty.call(value,subkey))clean[key][subkey]=check(`${key}.${subkey}`,value[subkey],defaults[key][subkey]);}else clean[key]=check(key,value,defaults[key]);}
    return {id:presetId(),name:String(item.name||'Imported recipe').slice(0,60),settings:clean};
  }
  async function openSettings(){
    if($('setupDialog').open)$('setupDialog').close();
    if(!$('settingsDialog').open)$('settingsDialog').showModal();
    await refreshStatus();await refreshDependencies();
  }
  function selectSourceTab(tab){$$('[data-source-tab]').forEach(b=>b.classList.toggle('active',b.dataset.sourceTab===tab));$('uploadArea').hidden=tab!=='upload';$('pathArea').hidden=tab!=='path';}
  async function loadSource(source,name){
    const request=++sourceRequest;sourceLoading=true;metadata=null;updateRecipe();recipeRevision++;settings.source=source;$('sourceBadge').textContent='READING SOURCE';$('sourceWarnings').textContent='';
    try{const found=await api('/api/probe',{source});if(request!==sourceRequest)return;metadata=found;}catch(error){if(request!==sourceRequest)return;metadata=null;$('sourceWarnings').textContent=`Metadata unavailable: ${error.message}`;}
    sourceLoading=false;$('sourceSection').classList.add('loaded');$('sourceLoaded').hidden=false;$('uploadArea').hidden=true;$('pathArea').hidden=true;
    const sourcePath=metadata?.path||source.path||source.paths[0];const frameCount=metadata?.frameCount??source.paths.length;$('sourceName').textContent=name||(source.kind==='sequence'?`${frameCount} PNG frames`:metadata?.name||sourcePath.split(/[\\/]/).pop());$('sourcePathDisplay').textContent=source.kind==='sequence'?`${frameCount} PNG frames · ${source.path||source.paths[0]}`:sourcePath;
    $('sourceBadge').textContent=source.kind==='sequence'?`${frameCount} FRAMES`:'SOURCE READY';
    const url='/api/source?path='+encodeURIComponent(sourcePath);const image=/\.(png|gif|jpe?g|webp|bmp)$/i.test(sourcePath)||source.kind==='sequence';
    $('sourceVideo').pause();$('sourceVideo').removeAttribute('src');$('sourceVideo').load();$('sourceVideo').hidden=image;$('sourceImage').hidden=!image;$('previewFallback').hidden=true;
    if(image){$('sourceImage').src=url;}else{$('sourceVideo').src=url;$('sourceVideo').load();}
    const width=metadata?.width??metadata?.video?.width,height=metadata?.height??metadata?.video?.height;
    const fps=metadata?.fps??metadata?.frameRate??metadata?.frame_rate;
    const rows=[['DIMENSIONS',width&&height?`${width} × ${height}`:'—'],['DURATION',seconds(metadata?.duration??metadata?.format?.duration)],['SOURCE RATE',fps?`${Number(fps).toFixed(2)} fps`:'—'],['SOURCE SIZE',bytes(metadata?.size??metadata?.bytes??metadata?.format?.size)]];
    $('metadataGrid').replaceChildren(...rows.map(([label,value])=>{const n=el('div');n.append(el('span','',label),el('strong','',value));return n;}));
    if(width&&height&&!settings.crop.enabled){settings.crop.width=width;settings.crop.height=height;}
    for(const key of sourceMatches.keys()){const value=sourceValue(key);if(value!==null)set(settings,key,value);}
    syncFields();
    const warnings=metadata?.warnings;if(warnings)$('sourceWarnings').textContent=Array.isArray(warnings)?warnings.join(' '):String(warnings);
    if(source.kind==='sequence')$('sourceWarnings').textContent+=' Frame order is naturally sorted unless “Keep supplied PNG frame order” is enabled.';
    updateRecipe();await refreshPlan();
  }
  async function uploadFiles(files){
    if(uploadBusy)throw new Error('Wait for the current source to finish loading.');const list=Array.from(files);if(!list.length)return;if(list.length>1&&!list.every(f=>/\.png$/i.test(f.name)))throw new Error('Select one video or a sequence containing only PNG frames.');
    uploadBusy=true;updateRecipe();$('loadPath').disabled=true;$('uploadArea').setAttribute('aria-busy','true');const strong=$('uploadArea').querySelector('strong');const old=strong.textContent;const paths=[];
    try{for(let i=0;i<list.length;i++){strong.textContent=`Preparing ${i+1} of ${list.length}…`;const data=await api('/api/upload?name='+encodeURIComponent(list[i].name),undefined,{method:'POST',headers:{'Content-Type':'application/octet-stream'},body:list[i],timeoutMs:600000});paths.push(data.path);}
      const sequence=list.length>1;await loadSource({kind:sequence?'sequence':'video',path:sequence?'':paths[0],paths:sequence?paths:[]},sequence?`${list.length} PNG frames`:list[0].name);
    }finally{uploadBusy=false;$('loadPath').disabled=false;strong.textContent=old;$('uploadArea').removeAttribute('aria-busy');$('fileInput').value='';updateRecipe();}
  }
  function sourceError(){if(!sourceExists())return;$('sourceVideo').hidden=true;$('sourceImage').hidden=true;$('previewFallback').hidden=false;}
  async function refreshStatus(force=false){
    const request=++statusRequest;const nextState=await api(force===true?'/api/status/refresh':'/api/status',force===true?{}:undefined);if(request!==statusRequest)return;toolsState=nextState;const tools=toolsState.tools||{};const missing=Object.entries(tools).filter(([,v])=>!v.available).map(([k])=>k);
    const status=$('engineStatus');status.textContent=missing.length?`Missing: ${missing.join(', ')} · Settings`:'Local engines ready';status.classList.toggle('ready',!missing.length);status.classList.toggle('missing',!!missing.length);
    for(const name of ['gifski','ffmpeg','ffprobe']){
      const tool=tools[name]||{};let row=$('tool-row-'+name),input=$('path-'+name);
      if(!row){
        row=el('div','tool-field');row.id='tool-row-'+name;
        const head=el('div','tool-field-head');head.append(el('strong','',name),el('span','tool-state'));
        const label=el('label','',`${name} executable`);input=el('input');input.type='text';input.id='path-'+name;
        label.htmlFor=input.id;input.placeholder=name==='gifski'?'C:/Tools/gifski.exe':`C:/ffmpeg/bin/${name}.exe`;
        input.addEventListener('input',()=>toolDrafts.set(name,input.value));
        label.append(input);row.append(head,label,el('p','tool-version'));$('toolFields').append(row);
      }
      const badge=row.querySelector('.tool-state');badge.textContent=tool.available?'● Available':'○ Not found';badge.classList.toggle('missing',!tool.available);
      if(!toolDrafts.has(name))input.value=toolsState.config?.[name]||'';
      const origin=tool.origin||tool.detectionSource||tool.source;
      row.querySelector('.tool-version').textContent=[tool.version||tool.error,tool.path?`${origin?({path:'PATH',local:'App folder',config:'Explicit path',explicit:'Explicit path',portable:'App folder'}[origin]||origin)+' · ':''}${tool.path}`:null].filter(Boolean).join('\n')||'Add an executable path or place the tool on PATH.';
    }
    $('toolHelp').replaceChildren(...['gifski','ffmpeg','ffprobe'].map(name=>{const panel=el('details','panel tool-help');panel.append(el('summary','',`${name} · installed version & CLI help`),el('pre','code-block',[tools[name]?.version,tools[name]?.help].filter(Boolean).join('\n\n')||'No help output available. Check the executable path above.'));return panel;}));
  }
  function planText(plan){if(typeof plan==='string')return plan;if(Array.isArray(plan)){if(plan.every(v=>typeof v==='string'||typeof v==='number'))return plan.map(v=>/\s/.test(String(v))?JSON.stringify(v):String(v)).join(' ');return plan.map(planText).join('\n\n');}if(!plan)return '';if(plan.display)return plan.display;if(plan.argv)return plan.argv.map(v=>/\s/.test(String(v))?JSON.stringify(v):String(v)).join(' ');if(plan.commands)return planText(plan.commands);return JSON.stringify(plan,null,2);}
  async function refreshPlan(){
    const request=++planRequest,revision=recipeRevision;
    if(!sourceExists()){$('commandOutput').textContent='Load a source to inspect the encoding plan.';return;}
    try{const plan=await api('/api/plan',{settings:clone(settings)});if(request!==planRequest||revision!==recipeRevision)return;$('commandOutput').textContent=planText(plan);const notes=[...(plan.warnings||[]),...(plan.notes||[])];if(notes.length)$('commandOutput').textContent+='\n\nNotes:\n'+notes.join('\n');}
    catch(error){if(request===planRequest&&revision===recipeRevision)$('commandOutput').textContent='Plan unavailable: '+error.message;}
  }
  function schedulePlan(){clearTimeout(planTimer);if(selectedMode==='advanced'&&$('commandSection').open)planTimer=setTimeout(refreshPlan,500);}
  function validateSettings(){
    if(!sourceExists())throw new Error('Choose a source file first.');
    for(const n of $$('[data-key]'))if(!n.disabled&&!n.checkValidity())throw new Error(`Check the ${n.dataset.key} field in ${n.closest('#advancedControls')?'Advanced':'Custom'}.`);
    if(settings.trim.end!==null&&settings.trim.end<=settings.trim.start)throw new Error('The trim end must be later than the start.');
    if(settings.fixedColors.some(c=>!/^#?[a-f\d]{6}$/i.test(c)))throw new Error('Fixed palette colors must contain six hexadecimal digits, for example FF7957.');
    if(settings.fixedColors.length>255)throw new Error('Gifski accepts at most 255 fixed palette colors.');
    if(settings.matte&&!/^#?[a-f\d]{6}$/i.test(settings.matte))throw new Error('Matte color must contain six hexadecimal digits.');
    if(settings.source.kind==='sequence'&&settings.fps*settings.speed>100)throw new Error('For a PNG sequence, frame rate × speed must not exceed 100 fps.');
    if(settings.pipeline==='y4m'&&settings.bounce)throw new Error('Bounce requires the Automatic or PNG pipeline.');
    if(adaptive()&&selectedMode!=='discord'&&(!$('targetMB').checkValidity()||!Number.isFinite(settings.smart.targetMB)))throw new Error('Enter a file-size limit from 0.01 to 100,000 MB.');
  }
  async function runJob(requestedMode){
    if(submitting||sourceLoading||uploadBusy)return;validateSettings();const mode=typeof requestedMode==='string'?requestedMode:adaptive()?selectedMode:'encode';
    const revision=recipeRevision;jobSelectionRequest++;submitting=true;updateRecipe();
    try{
      const data=await api('/api/jobs',{settings:clone(settings),mode});currentJob=data.job||data;jobRevisions.set(currentJob.id,revision);
      if(mode==='estimate'){estimatePendingId=currentJob.id;latestEstimateJob=currentJob;}
      resultSignature='';renderJob();await pollJobs();
    }finally{submitting=false;updateRecipe();}
  }
  function activeStatus(status){return ['queued','running','preparing','encoding','pending'].includes(status);}
  function schedulePoll(){clearTimeout(pollTimer);pollTimer=setTimeout(pollJobs,document.hidden?10000:jobs.some(j=>activeStatus(j.status))?1000:4000);}
  async function pollJobs(){
    if(pollBusy)return;clearTimeout(pollTimer);pollBusy=true;
    try{
      const data=await api('/api/jobs?summary=1',undefined,{timeoutMs:15000});jobs=Array.isArray(data)?data:data.jobs||[];
      if(!currentJob&&jobs.length){const ordered=jobs.slice().sort((a,b)=>Number(b.created)-Number(a.created));currentJob=ordered.find(j=>activeStatus(j.status))||ordered[0];}
      const selectedId=currentJob?.id,ids=[...new Set([selectedId,estimatePendingId].filter(Boolean))];
      const results=await Promise.allSettled(ids.map(id=>api('/api/jobs/'+encodeURIComponent(id),undefined,{timeoutMs:15000})));
      let detailError=null;
      results.forEach((result,index)=>{if(result.status!=='fulfilled'){detailError=result.reason;return;}const job=result.value.job||result.value;
        if(currentJob?.id===ids[index]){currentJob=job;renderJob();}
        if(job.id===estimatePendingId){latestEstimateJob=job;if(!activeStatus(job.status))estimatePendingId=null;}
      });
      renderQueue();renderEstimate();updateWakeLock();
      if(detailError)throw detailError;
      $('connectionStatus').hidden=true;
    }catch(error){$('connectionStatus').hidden=false;$('connectionStatus').textContent='Live updates interrupted. Keep the launcher window open. Retrying automatically. '+(error.message||'');}
    finally{pollBusy=false;schedulePoll();}
  }
  function renderJob(){
    if(!currentJob)return;const job=currentJob,running=activeStatus(job.status),process=job.process||{};
    $('jobIdle').hidden=true;$('jobActivity').hidden=false;
    $('jobMode').textContent=({encode:'ENCODE',sweep:'PREVIOUS COMPARISON',optimize:'PREVIOUS SIZE SEARCH',estimate:'SIZE ESTIMATE',smart:'SMART SIZE SEARCH',discord:'DISCORD · 20 MB SEARCH'})[job.mode]||'ENCODING JOB';
    $('jobStage').textContent=job.stage||job.status||'Queued';$('jobStatus').textContent=String(job.status||'queued').replaceAll('_',' ');$('cancelJob').hidden=!running;
    const processActive=running&&process.status==='running',raw=processActive?process.percent:job.progress,progress=Number(raw),known=raw!==null&&raw!==undefined&&Number.isFinite(progress);
    $('jobProgress').classList.toggle('indeterminate',!known&&running);$('jobProgress').style.width=(known?Math.max(0,Math.min(100,progress)):job.status==='completed'?100:0)+'%';
    $('jobPercent').textContent=known?`${Math.round(progress)}%${processActive?' of stage':''}`:running?'Working…':'';
    if(known)$('jobProgressTrack').setAttribute('aria-valuenow',Math.round(Math.max(0,Math.min(100,progress))));else $('jobProgressTrack').removeAttribute('aria-valuenow');
    $('jobProgressTrack').setAttribute('aria-valuetext',known?$('jobPercent').textContent:running?'Working; progress unavailable':job.status);
    const stats=[];if(process.tool)stats.push(String(process.tool));if(process.elapsedSeconds!=null)stats.push(seconds(process.elapsedSeconds)+' elapsed');
    if(process.frame!=null)stats.push(Number(process.frame).toLocaleString()+' frames');if(Number(process.fps)>0)stats.push(Number(process.fps).toFixed(1)+' fps');
    if(process.speed)stats.push(String(process.speed).replace(/x$/,'')+'× speed');$('processStats').textContent=stats.join(' · ');$('processStats').hidden=!stats.length;
    $('processHint').hidden=!processActive;$('processHint').classList.toggle('process-stalled',!!process.stalled);
    $('processHint').textContent=process.stalled?'No new progress reported recently. The process is still running; large or complex clips can take longer. You can cancel if needed.':String(process.tool).toLowerCase()==='gifski'&&!known?'Gifski is encoding on the CPU. Some builds report only completion; elapsed time and the log stay live.':'Preparing frames and encoding locally. Progress is reported by the active process.';
    $('jobError').hidden=!job.error;$('jobError').textContent=job.error||'';$('jobWarnings').hidden=!job.warnings?.length;$('jobWarnings').textContent=(job.warnings||[]).join(' ');
    const logs=Array.isArray(job.logs)?job.logs.join('\n'):String(job.logs||'');if($('jobLogs').textContent!==(logs||'Waiting for process output…')){
      const position=$('jobLogs').scrollTop;$('jobLogs').textContent=logs||'Waiting for process output…';$('jobLogs').scrollTop=$('followLogs').checked?$('jobLogs').scrollHeight:position;
    }
    $('searchStatus').hidden=!job.search;$('searchStatus').textContent=job.search?`${job.search.sampleTrials||0} sample ${job.search.sampleTrials===1?'check':'checks'} · ${job.search.fullTrials||0} full ${job.search.fullTrials===1?'encode':'encodes'}${job.search.cacheHits?' · '+job.search.cacheHits+' cache hits':''}`:'';
    if(job.mode==='estimate'&&job.estimate){latestEstimateJob=job;}
    renderEstimate();renderResults(job);
  }
  function resultURL(result,job,download=false){return `/api/files/${encodeURIComponent(job.id)}/${result.index??job.results.indexOf(result)}${download?'?download=1':''}`;}
  function allResults(job){return (job.results||[]).map((result,i)=>({...result,index:result.index??i}));}
  let resultSignature='';
  function renderResults(job){
    const results=allResults(job).filter(result=>job.mode!=='estimate'||!result.sample);$('resultsSection').hidden=!results.length;if(!results.length)return;
    const signature=job.id+'|'+job.status+'|'+job.bestIndex+'|'+results.map(r=>`${r.index}:${r.bytes}:${r.fit}`).join(',');if(resultSignature===signature)return;resultSignature=signature;
    const budget=Number(job.targetBytes)||0,isSearch=['smart','discord','optimize'].includes(job.mode);
    const complete=results.filter(r=>!r.sample);
    const best=results.find(r=>r.index===job.bestIndex)||results.find(r=>r.best)||complete.filter(r=>budget>0&&r.bytes<=budget).sort((a,b)=>(b.score||0)-(a.score||0))[0]||complete.slice().sort((a,b)=>a.bytes-b.bytes)[0]||results[0];
    const alternatives=results.filter(r=>r!==best);$('resultCount').textContent=`${complete.length} FULL OUTPUT${complete.length===1?'':'S'}${results.length!==complete.length?' · '+(results.length-complete.length)+' SAMPLES':''}`;
    $('resultSummary').textContent=job.mode==='estimate'?(job.estimate?.exact?'Full trim range measured.':'Sample-based estimate. Sample downloads contain only the tested intervals.'):isSearch?`${activeStatus(job.status)?'Search in progress. ':''}${best.bytes<=budget&&!best.sample?'The selected full GIF fits':activeStatus(job.status)?'No tested full GIF fits yet':'No tested full GIF fits'} ${budget/1000000} MB.${!activeStatus(job.status)&&best.bytes<=budget?' The best tested fit is selected.':''}`:'Your complete GIF is ready to preview and download.';
    $('searchDetails').hidden=!job.searchNote;$('searchDetailsText').textContent=job.searchNote||'';
    $('bestResult').replaceChildren(resultCard(best,job,true,budget));
    $('alternatives').hidden=!alternatives.length;$('alternativesLabel').textContent=`Other tested outputs (${alternatives.length})`;
    $('resultsGrid').replaceChildren(...alternatives.map(r=>resultCard(r,job,false,budget)));
  }
  function resultCard(r,job,primary,budget){
    const fit=!r.sample&&budget>0&&r.bytes<=budget,card=el('article','candidate'+(primary?' primary-result':'')+(fit?' fits':''));
    const stage=el('div','candidate-image'),img=el('img');img.src=resultURL(r,job);img.alt=`GIF output ${r.index+1}`;img.loading='lazy';stage.append(img);
    const body=el('div','candidate-body'),title=el('div','candidate-title');title.append(el('strong','',budget>0?`${(r.bytes/1000000).toFixed(2)} MB`:bytes(r.bytes)),el('span','',r.sample?'SAMPLE':budget>0?(fit?'WITHIN LIMIT':'OVER LIMIT'):'MEASURED'));
    const width=r.width??r.settings?.width??'auto',height=r.height??r.settings?.height;
    body.append(title,el('div','candidate-info',`${width}${height?' × '+height:' px'} · ${r.fps??r.settings?.fps??'—'} fps · Q${r.quality??r.settings?.quality??'—'}`));
    if(budget>0){const track=el('div','budget-track'),fill=el('div',fit?'':'over');fill.style.width=Math.min(100,r.bytes/budget*100)+'%';track.append(fill);body.append(track,el('p','hint',`${(r.bytes/1000000).toFixed(3)} / ${budget/1000000} MB · ${r.bytes.toLocaleString()} bytes`));}
    const actions=el('div','candidate-actions'),download=el('a','button '+(primary?'primary':'secondary'),r.sample?'Download sample':'Download GIF');download.href=resultURL(r,job,true);download.download=r.filename||'animation.gif';
    actions.append(download,button('Use settings','button subtle',()=>{const source=clone(settings.source),next=clone(r.settings||job.settings||{});settings={...clone(defaults),...next,source};for(const k of ['crop','smart','trim','performance'])settings[k]={...defaults[k],...next[k]};if(r.sample)settings.trim=clone(job.settings.trim);selectedMode='advanced';selectedPreset=null;sourceMatches.clear();recipeRevision++;syncFields();schedulePlan();notice('Output settings loaded in Advanced.');}));
    body.append(actions);card.append(stage,body);return card;
  }
  function renderQueue(){
    $('queueCount').textContent=jobs.length;
    if(!jobs.length){if(!$('queueList').querySelector('.queue-empty'))$('queueList').replaceChildren(el('div','queue-empty','Your recent jobs will appear here.'));return;}
    $('queueList').querySelector('.queue-empty')?.remove();const ordered=jobs.slice().sort((a,b)=>Number(b.created)-Number(a.created));
    const retained=new Set(ordered.map(job=>job.id));for(const card of [...$('queueList').children])if(!retained.has(card.dataset.jobId))card.remove();
    ordered.forEach((job,index)=>{
      let card=[...$('queueList').children].find(node=>node.dataset.jobId===job.id);
      if(!card){card=el('article','queue-card');card.dataset.jobId=job.id;const content=el('div','queue-content');content.append(el('span','queue-status'),el('h3'),el('p'));const actions=el('div','queue-actions');
        const open=button('View','button secondary compact',safeRun(async()=>{const request=++jobSelectionRequest;const detail=await api('/api/jobs/'+encodeURIComponent(job.id),undefined,{timeoutMs:15000});if(request!==jobSelectionRequest)return;currentJob=detail.job||detail;resultSignature='';renderJob();renderQueue();}));
        open.setAttribute('aria-label',`View ${job.sourceName||'job'} details`);actions.append(open,button('Cancel','button subtle compact',safeRun(async()=>{await api('/api/jobs/'+encodeURIComponent(job.id)+'/cancel',{});await pollJobs();})));card.append(content,actions);
      }
      card.classList.toggle('selected',job.id===currentJob?.id);card.querySelector('.queue-status').textContent=job.status;
      card.querySelector('h3').textContent=job.sourceName||job.settings?.source?.path?.split(/[\\/]/).pop()||'Animation';
      card.querySelector('p').textContent=`${job.mode||'encode'} · ${job.resultCount??job.results?.length??0} outputs`;
      card.querySelector('.queue-actions').lastChild.hidden=!activeStatus(job.status);card.querySelector('.queue-actions').firstChild.setAttribute('aria-pressed',String(job.id===currentJob?.id));
      if($('queueList').children[index]!==card)$('queueList').insertBefore(card,$('queueList').children[index]||null);
    });
  }
  function sourceValue(key){
    if(!metadata)return null;
    const raw=key==='trim.end'?metadata.duration:key==='fps'?(settings.source.kind==='sequence'?null:metadata.fps):metadata[key];
    const value=Number(raw);if(raw==null||!Number.isFinite(value)||value<=0)return null;
    return key==='fps'?Math.round(Math.min(50,value)*1000)/1000:key==='width'||key==='height'?Math.round(value):Math.round(value*100)/100;
  }
  function updateSourceMatches(){
    $$('[data-match]').forEach(b=>{const key=b.dataset.match;const value=sourceValue(key);b.setAttribute('aria-label',`Match source ${key==='trim.end'?'duration':key==='fps'?'frame rate':key}`);b.disabled=value===null||(adaptive()&&key!=='trim.end');b.setAttribute('aria-pressed',String(sourceMatches.has(key)));b.title=value===null?'Load a source with this metadata first.':`Match source: ${key==='fps'?value.toFixed(3)+' fps':key==='trim.end'?seconds(value):value+' px'}${key==='fps'&&metadata.fps>50?' (capped at 50 fps for GIF)':''}`;});
  }
  function toggleSourceMatch(key){
    if(sourceMatches.has(key)){set(settings,key,sourceMatches.get(key));sourceMatches.delete(key);}
    else{const value=sourceValue(key);if(value===null)return;sourceMatches.set(key,get(settings,key));set(settings,key,value);
      if(key==='width'||key==='height'){const other=key==='width'?'height':'width';sourceMatches.delete(other);set(settings,other,null);}
    }
    changed(key);syncFields();
  }
  function renderEstimate(){
    let estimate=latestEstimateJob?.estimate,job=latestEstimateJob,label='ESTIMATED SIZE',value='Not estimated',state='',range='Run a quick estimate before encoding.',note='Short samples give an approximate range. A full encode verifies the final size.';
    if(adaptive()){
      job=currentJob&&['smart','discord'].includes(currentJob.mode)?currentJob:null;estimate=job?.estimate;
      label='SIZE LIMIT';value=`≤ ${selectedMode==='discord'?20:settings.smart.targetMB} MB`;range='The search measures complete GIFs to verify your limit.';note='Samples guide the choice of dimensions, frame rate, and quality. The final result shows the best tested fit.';
      const best=job?.results?.find(result=>result.index===job.bestIndex&&!result.sample);
      if(best&&job.status==='completed'){label='MEASURED SIZE';value=bytes(best.bytes);range=`${best.bytes.toLocaleString()} bytes · ${Number(job.targetBytes)/1000000} MB limit`;note='Complete output measured. See the result for whether it fits the requested limit.';}
      else if(estimate){label='LATEST TRIAL';value='≈ '+bytes(estimate.estimatedBytes);range=`${bytes(estimate.lowerBytes)} – ${bytes(estimate.upperBytes)} sampled range`;note='This is a candidate in the ongoing search. The final selected GIF may use different settings and is measured in full.';}
      if(job&&activeStatus(job.status))state='Searching';
    }else if(estimate){
      label=estimate.exact?'MEASURED SIZE':'ESTIMATED SIZE';value=(estimate.exact?'':'≈ ')+bytes(estimate.estimatedBytes);
      range=estimate.exact?'Full trim range encoded.':`${bytes(estimate.lowerBytes)} – ${bytes(estimate.upperBytes)} approximate range · ${estimate.sampleCount} samples`;
      note=estimate.note||note;state=estimate.cacheHit?'Cached':'';
    }
    if(!adaptive()&&job&&activeStatus(job.status)){state='Calculating';if(!estimate)value='Calculating…';}
    if(job&&jobRevisions.get(job.id)!==recipeRevision){state=state?state+' · stale':'Stale';note+=' Settings have changed since this job; run again to update.';}
    if(job&&['failed','cancelled'].includes(job.status)){state=job.status==='failed'?'Failed':'Cancelled';note=job.error||'The estimate was cancelled. You can start it again.';}
    $('estimateLabel').textContent=label;$('estimateValue').textContent=value;$('estimateRange').textContent=range;$('estimateNote').textContent=note;
    $('estimateStale').hidden=!state;$('estimateStale').textContent=state;$('estimateSummary').classList.toggle('working',!!job&&activeStatus(job.status));
    $('estimateJob').textContent=estimatePendingId?'Estimating…':'Estimate size';$('estimateJob').disabled=!!estimatePendingId||!sourceExists()||submitting||uploadBusy||sourceLoading;
  }
  async function updateWakeLock(){
    if(!('wakeLock' in navigator))return;
    const wanted=$('keepAwake').checked&&!document.hidden&&jobs.some(job=>activeStatus(job.status));
    if(!wanted){if(wakeLock){const old=wakeLock;wakeLock=null;await old.release().catch(()=>{});}$('wakeLockStatus').textContent='Optional. Keeps the screen awake while a job runs and this page is visible.';return;}
    if(wakeLock||wakeRequestPending||wakeDenied)return;wakeRequestPending=true;
    try{
      const acquired=await navigator.wakeLock.request('screen');wakeLock=acquired;
      acquired.addEventListener('release',()=>{if(wakeLock===acquired)wakeLock=null;});
      if(!$('keepAwake').checked||document.hidden||!jobs.some(job=>activeStatus(job.status))){wakeLock=null;await acquired.release();return;}
      $('wakeLockStatus').textContent='Screen wake lock active while a job runs and this page is visible.';
    }catch{wakeDenied=true;$('wakeLockStatus').textContent='This browser could not keep the screen awake. Encoding continues normally.';}
    finally{wakeRequestPending=false;}
  }
  function allToolsReady(){return ['gifski','ffmpeg','ffprobe'].every(name=>toolsState?.tools?.[name]?.available);}
  async function refreshDependencies(){
    if(dependencyBusy)return;dependencyBusy=true;
    try{dependencyState=await api('/api/dependencies');renderDependencies();if(dependencyState.busy||dependencyState.updates?.state==='checking'){if(!dependencyPoll)dependencyPoll=setInterval(()=>refreshDependencies().catch(showSetupError),1200);}else if(dependencyPoll||Object.values(dependencyState.tools||{}).some(t=>t.state==='completed')){clearInterval(dependencyPoll);dependencyPoll=null;await refreshStatus();renderDependencies();}}
    finally{dependencyBusy=false;}
  }
  function showSetupError(error){const message=error.message||String(error);$('setupError').hidden=false;$('setupError').textContent=message;if($('settingsDialog').open)$('versionCheckStatus').textContent=message;}
  function renderDependencies(){
    const state=dependencyState;if(!state)return;
    $('installMissing').hidden=!state.supported||allToolsReady();$('installMissing').disabled=!!state.busy;$('installMissing').textContent=state.busy?'Setting up tools…':'Set up missing tools';
    $('setupProgress').hidden=!state.setup?.message;$('setupProgress').textContent=state.setup?.message||'';
    $('setupPlatform').textContent=`${state.platform||'This system'} · ${state.supported?'Portable installation available.':'Automatic installation is not available on this platform. Use the guidance below or existing executable paths.'}`;
    $('dependencyCards').replaceChildren(...['gifski','ffmpeg'].map(name=>{
      const item=state.tools?.[name]||{},ready=!!toolsState?.tools?.[name]?.available&&(name!=='ffmpeg'||!!toolsState?.tools?.ffprobe?.available),installing=['queued','installing'].includes(item.state);
      const card=el('div','dependency-card'),heading=el('div','dependency-heading');heading.append(el('strong','',name==='ffmpeg'?'FFmpeg + FFprobe':'Gifski'),el('span','tool-state'+(ready?'':' missing'),ready?'Ready':item.state==='queued'?'Queued':installing?'Installing…':item.state==='failed'?'Installation failed':'Missing'));card.append(heading);
      const update=state.updates?.tools?.[name]?.state==='update_available';
      const install=button(installing?'Installing…':ready?(update?'Update portable copy':'Installed'):'Install','button secondary compact',safeRun(async()=>{install.disabled=true;$('setupError').hidden=true;try{dependencyState=await api('/api/dependencies/install',{tool:name});renderDependencies();await refreshDependencies();}catch(error){showSetupError(error);await refreshDependencies();}}));install.disabled=(ready&&!update)||!!state.busy||!state.supported;card.append(install);
      if(ready&&update)card.append(el('p','hint','Updates the portable copy in bin/. An explicit path or PATH installation still takes priority; set the portable executable path to use it.'));
      if(item.message)card.append(el('p','hint',item.message));if(item.error)card.append(el('p','error-text',item.error));
      if(installing){const meter=el('progress');meter.max=100;meter.value=Math.max(0,Math.min(100,Number(item.progress)||0));meter.setAttribute('aria-label',`${name} installation progress`);card.append(meter);}
      if(!state.supported&&state.manual?.[name])card.append(el('pre','code-block',typeof state.manual[name]==='string'?state.manual[name]:JSON.stringify(state.manual[name],null,2)));
      return card;
    }));renderVersionChecks();updateSetup();
  }
  function updateSetup(){
    $('setupNext').disabled=!allToolsReady();
    $('setupReadyNote').textContent=allToolsReady()?'All tools are ready.':'Connect all three tools to get started.';
  }
  function renderVersionChecks(){
    const updates=dependencyState?.updates;if(!updates)return;
    const checking=versionRequestPending||updates.state==='checking';$('checkVersions').disabled=checking;$('checkVersions').textContent=checking?'Checking…':'Check for updates';
    $('versionCheckStatus').textContent=checking?'Checking official release sources…':updates.message?updates.message:updates.checkedAt?`${updates.cached?'Cached check':'Last checked'} · ${new Date(updates.checkedAt*1000).toLocaleString()}`:'No online check performed yet.';
    const signature=JSON.stringify(updates.tools||{});if(signature===versionSignature)return;versionSignature=signature;
    $('versionChecks').replaceChildren(...Object.entries(updates.tools||{}).map(([name,item])=>{const row=el('div','version-row'),heading=el('div','version-row-head');heading.append(el('strong','',name),el('span','tag',String(item.state||'not_checked').replaceAll('_',' ')));row.append(heading,el('p','hint',`${item.installedVersion?'Installed '+item.installedVersion:'No installed version'}${item.latestVersion?' · Latest '+item.latestVersion:''}`));if(item.message)row.append(el('p',item.errorCode?'error-text':'hint',item.message));if(item.errorCode)row.append(el('p','hint','Diagnostic: '+item.errorCode));if(item.sourceUsed)row.append(el('p','hint','Source: '+item.sourceUsed));if(item.sourceUrl){try{const url=new URL(item.sourceUrl);if(url.protocol==='https:'){const link=el('a','text-button','Official release source ↗');link.href=url.href;link.target='_blank';link.rel='noreferrer';row.append(link);}}catch{}}return row;}));
  }
  async function openSetup(){if($('settingsDialog').open)$('settingsDialog').close();$('setupError').hidden=true;updateSetup();if(!$('setupDialog').open)$('setupDialog').showModal();try{await refreshStatus();await refreshDependencies();updateSetup();}catch(error){showSetupError(error);}}
  async function checkSetup(){if(setupChecked)return;setupChecked=true;if(!allToolsReady())await openSetup();}
  $$('[data-match]').forEach(b=>b.addEventListener('click',event=>{event.preventDefault();toggleSourceMatch(b.dataset.match);}));
  $('estimateJob').addEventListener('click',safeRun(()=>runJob('estimate')));
  $('openSetup').addEventListener('click',safeRun(openSetup));
  $('closeSetup').addEventListener('click',()=>$('setupDialog').close());
  $('setupNext').addEventListener('click',()=>{if(allToolsReady()){$('setupDialog').close();}});
  $('setupManual').addEventListener('click',()=>{$('setupDialog').close();safeRun(openSettings)();});
  $('setupRecheck').addEventListener('click',safeRun(async()=>{await refreshStatus(true);await refreshDependencies();updateSetup();}));
  $$('[data-key]').forEach(node=>node.addEventListener(node.type==='checkbox'||node.tagName==='SELECT'?'change':'input',()=>{let value=node.type==='checkbox'?node.checked:node.type==='number'||node.type==='range'?Number(node.value):node.value;if(node.dataset.null&&node.value==='')value=null;sourceMatches.delete(node.dataset.key);set(settings,node.dataset.key,value);if(node.dataset.key==='fps')$('sequenceRate').value=value;changed(node.dataset.key);}));
  $('openSettings').addEventListener('click',safeRun(openSettings));$('closeSettings').addEventListener('click',()=>$('settingsDialog').close());
  $('closeEstimate').addEventListener('click',()=>$('estimateSummary').open=false);
  document.addEventListener('keydown',event=>{if(event.key==='Escape'&&$('estimateSummary').open)$('estimateSummary').open=false;});
  document.addEventListener('click',event=>{if($('estimateSummary').open&&!$('estimateSummary').contains(event.target))$('estimateSummary').open=false;});
  $('followLogs').addEventListener('change',()=>{if($('followLogs').checked)$('jobLogs').scrollTop=$('jobLogs').scrollHeight;});
  $('processLog').addEventListener('toggle',()=>{if($('processLog').open&&$('followLogs').checked)$('jobLogs').scrollTop=$('jobLogs').scrollHeight;});
  $('copyLogs').addEventListener('click',safeRun(async()=>{await navigator.clipboard.writeText($('jobLogs').textContent);notice('Process log copied.');}));
  $$('[data-source-tab]').forEach(b=>b.addEventListener('click',()=>selectSourceTab(b.dataset.sourceTab)));
  $('dismissNotice').addEventListener('click',()=>{$('notice').hidden=true;});
  $('resetSettings').addEventListener('click',()=>{applyPreset(builtinPresets[0],'default');notice('Encoding settings reset.');});
  $('fixedColors').addEventListener('input',()=>{settings.fixedColors=$('fixedColors').value.split(/[\s,;]+/).filter(Boolean);changed('fixedColors');});
  $('loopMode').addEventListener('change',()=>{settings.repeat=$('loopMode').value==='once'?-1:$('loopMode').value==='forever'?0:1;$('repeatField').hidden=settings.repeat<=0;$$('[data-key="repeat"]')[0].disabled=settings.repeat<=0;$$('[data-key="repeat"]')[0].value=settings.repeat;changed('repeat');});
  $('savePreset').addEventListener('click',safeRun(()=>{$('presetName').value='';$('presetDialog').showModal();}));
  $('confirmPreset').addEventListener('click',event=>{if(!$('presetName').value.trim()){event.preventDefault();return;}try{const name=$('presetName').value.trim(),item=validateImported({name,settings:recipeForSave()});const existing=customPresets.findIndex(p=>p.name===name);if(existing>=0)customPresets[existing]=item;else{if(customPresets.length>=100)throw new Error('Delete or export a saved preset before adding another (100 maximum).');customPresets.push(item);}persistPresets();renderPresets();notice(`Saved “${name}”.`);}catch(error){event.preventDefault();notice(error.message,true);}});
  $('exportPreset').addEventListener('click',safeRun(()=>{const saved=validateImported({name:'My Gifski recipe',settings:recipeForSave()});const blob=new Blob([JSON.stringify({format:'gifski-webui-recipe',version:1,name:saved.name,settings:saved.settings},null,2)],{type:'application/json'});const url=URL.createObjectURL(blob),a=el('a');a.href=url;a.download='gifski-recipe.json';a.click();setTimeout(()=>URL.revokeObjectURL(url),1000);}));
  $('importPreset').addEventListener('change',safeRun(async()=>{const file=$('importPreset').files[0];if(!file)return;if(customPresets.length>=100)throw new Error('Delete a saved preset before importing another (100 maximum).');if(file.size>100000)throw new Error('Recipe files must be smaller than 100 KB.');const p=validateImported(JSON.parse(await file.text()));customPresets.push(p);persistPresets();applyPreset(p);$('importPreset').value='';notice('Recipe imported. Review settings before encoding.');}));
  $('fileInput').addEventListener('change',safeRun(e=>uploadFiles(e.target.files)));
  $('uploadArea').addEventListener('click',()=>$('fileInput').click());$('uploadArea').addEventListener('keydown',e=>{if(e.key==='Enter'||e.key===' '){e.preventDefault();$('fileInput').click();}});
  for(const name of ['dragenter','dragover'])$('uploadArea').addEventListener(name,e=>{e.preventDefault();$('uploadArea').classList.add('dragover');});
  for(const name of ['dragleave','drop'])$('uploadArea').addEventListener(name,e=>{e.preventDefault();$('uploadArea').classList.remove('dragover');});
  $('uploadArea').addEventListener('drop',safeRun(e=>uploadFiles(e.dataTransfer.files)));
  $('sourceKind').addEventListener('change',()=>{const sequence=$('sourceKind').value==='sequence';$('singlePathLabel').hidden=sequence;$('sequencePathLabel').hidden=!sequence;});
  $('loadPath').addEventListener('click',safeRun(async()=>{if(uploadBusy)return;const kind=$('sourceKind').value;const path=$('sourcePath').value.trim().replace(/^"|"$/g,'');const paths=$('sourcePaths').value.split('\n').map(s=>s.trim().replace(/^"|"$/g,'')).filter(Boolean);if(kind==='sequence'?!paths.length:!path)throw new Error('Enter a source path first.');const directory=kind==='sequence'&&paths.length===1&&!/\.png$/i.test(paths[0]);await loadSource({kind,path:directory?paths[0]:kind==='sequence'?'':path,paths:kind==='sequence'&&!directory?paths:[]});}));
  $('changeSource').addEventListener('click',()=>{sourceRequest++;sourceLoading=false;metadata=null;settings.source=clone(defaults.source);recipeRevision++;$('sourceSection').classList.remove('loaded');$('sourceLoaded').hidden=true;$('sourceBadge').textContent='NO SOURCE';$('sourceVideo').pause();$('sourceVideo').removeAttribute('src');$('sourceVideo').load();$('sourceVideo').hidden=true;$('sourceImage').removeAttribute('src');$('sourceImage').hidden=true;selectSourceTab('upload');updateRecipe();updateSourceMatches();});
  $('sourceVideo').addEventListener('error',sourceError);$('sourceImage').addEventListener('error',sourceError);
  $('markStart').addEventListener('click',()=>{settings.trim.start=Math.round(($('sourceVideo').currentTime||0)*100)/100;syncFields();changed('trim.start');});
  $('markEnd').addEventListener('click',()=>{const t=$('sourceVideo').currentTime;if(!t){notice('Move the video playhead to the desired end first.');return;}sourceMatches.delete('trim.end');settings.trim.end=Math.round(t*100)/100;syncFields();changed('trim.end');});
  $('saveTools').addEventListener('click',safeRun(async()=>{
    const config={},submitted={};for(const name of ['gifski','ffmpeg','ffprobe']){submitted[name]=$('path-'+name).value;config[name]=submitted[name].trim().replace(/^"|"$/g,'');}
    $('saveTools').disabled=true;statusRequest++;
    try{await api('/api/config',config);for(const name of Object.keys(submitted))if(toolDrafts.get(name)===submitted[name])toolDrafts.delete(name);await refreshStatus();notice(toolDrafts.size?'Saved. Newer edits remain unsaved.':'Executable paths saved and checked.');}
    finally{$('saveTools').disabled=false;}
  }));
  $('refreshTools').addEventListener('click',safeRun(()=>refreshStatus(true).then(refreshDependencies)));$('refreshPlan').addEventListener('click',refreshPlan);$('commandSection').addEventListener('toggle',()=>{if($('commandSection').open)refreshPlan();});
  $('copyPlan').addEventListener('click',safeRun(async()=>{await navigator.clipboard.writeText($('commandOutput').textContent);notice('Command plan copied.');}));
  $('runJob').addEventListener('click',safeRun(runJob));$('cancelJob').addEventListener('click',safeRun(async()=>{if(currentJob){await api('/api/jobs/'+encodeURIComponent(currentJob.id)+'/cancel',{});await pollJobs();}}));
  $('targetMB').addEventListener('input',()=>{settings.smart.targetMB=Number($('targetMB').value);changed('smart.targetMB');});
  $('searchEffort').addEventListener('change',()=>{settings.smart.effort=$('searchEffort').value;changed('smart.effort');});
  $('sequenceRate').addEventListener('input',()=>{settings.fps=Number($('sequenceRate').value);$$('[data-key="fps"]')[0].value=settings.fps;changed('sequenceRate');});
  $('processingSummary').addEventListener('click',()=>selectMode('advanced'));
  $('customizePreset').addEventListener('click',()=>selectMode('custom'));
  $('checkVersions').addEventListener('click',safeRun(async()=>{
    if(versionRequestPending)return;versionRequestPending=true;$('checkVersions').disabled=true;$('checkVersions').textContent='Checking…';$('versionCheckStatus').textContent='Checking installed tools and official release sources…';
    try{dependencyState=await api('/api/dependencies/check-updates',{});renderDependencies();await refreshDependencies();}
    catch(error){$('versionCheckStatus').textContent=error.message;dependencyState={...(dependencyState||{}),updates:{state:'failed',message:error.message,tools:dependencyState?.updates?.tools||{}}};throw error;}
    finally{versionRequestPending=false;renderVersionChecks();}
  }));
  $('installMissing').addEventListener('click',safeRun(async()=>{
    $('installMissing').disabled=true;$('setupError').hidden=true;
    try{dependencyState=await api('/api/dependencies/install-missing',{});renderDependencies();await refreshDependencies();}
    catch(error){showSetupError(error);await refreshDependencies();}
  }));
  $('keepAwake').addEventListener('change',()=>{wakeDenied=false;try{localStorage.setItem('gifski-keep-awake',String($('keepAwake').checked));}catch{}updateWakeLock();});
  try{$('keepAwake').checked=localStorage.getItem('gifski-keep-awake')==='true';}catch{}
  if(!('wakeLock' in navigator)){$('keepAwake').disabled=true;$('wakeLockStatus').textContent='Screen wake lock is unavailable in this browser. Encoding still works normally.';}
  document.addEventListener('visibilitychange',()=>{if(!document.hidden)wakeDenied=false;updateWakeLock();if(!document.hidden)pollJobs();else schedulePoll();});
  if('ResizeObserver' in window)new ResizeObserver(()=>document.documentElement.style.setProperty('--toolbar-height',$('actionbar').getBoundingClientRect().height+'px')).observe($('actionbar'));
  syncFields();refreshStatus().then(checkSetup).catch(e=>{notice(e.message,true);$('engineStatus').textContent='Local server unavailable';});pollJobs();

})();
