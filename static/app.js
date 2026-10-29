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
    sweep:{qualities:[70,80,90],widths:[480,640],fps:[15,20],motionQualities:[],lossyQualities:[]},
    optimize:{targetMiB:8,minQuality:40,maxQuality:95,minWidth:320,minFps:10,allowResize:true,allowFps:true,maxTrials:18}
  };
  const builtinPresets = [
    {id:'discord',name:'Discord 20 MB',icon:'◎',description:'Adaptive search from your source. Measures full GIFs and selects the best tested result at or below 20,000,000 bytes.',settings:{}},
    {id:'balanced',name:'Balanced',icon:'◈',description:'A versatile balance of detail, motion, and size.',settings:{width:640,fps:20,quality:90}},
    {id:'small',name:'Compact',icon:'◧',description:'Smaller dimensions and fewer frames, with room to tune quality upward.',settings:{width:480,fps:15,quality:75}},
    {id:'quality',name:'Fidelity',icon:'✳',description:'High detail and extra effort. Expect large files; compare with normal effort.',settings:{width:960,fps:25,quality:100,effort:'extra'}},
    {id:'ui',name:'UI & text',icon:'⌘',description:'Crisp interface details with a moderate frame rate. Inspect small text closely.',settings:{width:800,fps:15,quality:95,effort:'extra'}},
    {id:'motion',name:'Motion',icon:'≋',description:'More frames for fast action. Check GIF timing and the measured size.',settings:{width:640,fps:30,quality:90,motionQuality:95}},
    {id:'alpha',name:'Transparent',icon:'▦',description:'PNG preparation preserves alpha. GIF can only keep fully opaque or transparent pixels.',settings:{width:640,fps:20,quality:95,pipeline:'png'}}
  ];
  let settings = clone(defaults), currentTab='encode', toolsState=null, metadata=null, jobs=[], currentJob=null;
  let selectedPreset='balanced', pins={A:null,B:null}, actualPixels=false, pollBusy=false, noticeTimer=null, planTimer=null;
  let customPresets=[], discordEnabled=false, recipeRevision=0, estimateRevision=-1, lastEstimate=null;
  const sourceMatches=new Map(), toolDrafts=new Map();
  let statusRequest=0;
  let dependencyState=null, dependencyPoll=null, dependencyBusy=false, setupStep=0, setupChecked=false;
  try { customPresets=JSON.parse(localStorage.getItem('gifski-workbench-presets')||'[]'); if(!Array.isArray(customPresets)) customPresets=[]; } catch { customPresets=[]; }
  const get=(obj,key)=>key.split('.').reduce((a,k)=>a?.[k],obj);
  const set=(obj,key,value)=>{const path=key.split('.');const last=path.pop();let t=obj;for(const k of path)t=t[k];t[last]=value;};
  function el(tag,className,text){const node=document.createElement(tag);if(className)node.className=className;if(text!==undefined)node.textContent=String(text);return node;}
  function button(text,className,fn){const node=el('button',className,text);node.type='button';node.addEventListener('click',fn);return node;}
  function notice(message,error=false){$('noticeText').textContent=message;$('notice').classList.toggle('error',error);$('notice').hidden=false;clearTimeout(noticeTimer);noticeTimer=setTimeout(()=>{$('notice').hidden=true;},error?14000:5500);}
  async function api(path,body,opts={}){const response=await fetch(path,{credentials:'same-origin',...(body===undefined?{}:{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify(body)}),...opts});let data;const type=response.headers.get('content-type')||'';if(type.includes('json'))data=await response.json();else data={error:await response.text()};if(!response.ok)throw new Error(data.error||data.message||`Request failed (${response.status})`);return data;}
  const safeRun=fn=>(...args)=>Promise.resolve().then(()=>fn(...args)).catch(error=>notice(error.message||String(error),true));
  function bytes(value){if(!Number.isFinite(Number(value)))return '—';const n=Number(value);return n>=1048576?`${(n/1048576).toFixed(2)} MiB`:n>=1024?`${(n/1024).toFixed(1)} KiB`:`${n} B`;}
  function seconds(value){const n=Number(value);if(!Number.isFinite(n)||n<0)return '—';return n>=60?`${Math.floor(n/60)}:${String(Math.floor(n%60)).padStart(2,'0')}`:`${n.toFixed(2)} s`;}
  function sourceExists(){return !!(settings.source.path||settings.source.paths?.length);}
  function syncFields(){
    $$('[data-key]').forEach(node=>{const value=get(settings,node.dataset.key);if(node.type==='checkbox')node.checked=!!value;else node.value=value??'';});
    $('fixedColors').value=(settings.fixedColors||[]).join(', ');
    $('loopMode').value=settings.repeat===-1?'once':settings.repeat===0?'forever':'count';$('repeatField').hidden=settings.repeat<=0;$$('[data-key="repeat"]')[0].disabled=settings.repeat<=0;
    $('sweepQualities').value=settings.sweep.qualities.join(', ');$('sweepWidths').value=settings.sweep.widths.join(', ');$('sweepFps').value=settings.sweep.fps.join(', ');
    $('sweepMotion').value=(settings.sweep.motionQualities||[]).join(', ');$('sweepLossy').value=(settings.sweep.lossyQualities||[]).join(', ');
    for(const key of ['targetMiB','minQuality','maxQuality','minWidth','minFps','maxTrials'])$(key).value=settings.optimize[key];
    for(const key of ['allowResize','allowFps'])$(key).checked=settings.optimize[key];
    updateRecipe();renderPresets();updateTrialCount();updateSourceMatches();
  }
  function changed(){recipeRevision++;selectedPreset=discordEnabled?'discord':null;updateRecipe();updateSourceMatches();renderPresetSelection();schedulePlan();if(lastEstimate)$('estimateStale').hidden=false;}
  function updateRecipe(){
    if(lastEstimate)$('estimateStale').hidden=estimateRevision===recipeRevision;
    $('qualityValue').textContent=settings.quality;
    $('effortHint').textContent={normal:'Normal balances encoding time and fidelity.',extra:'Extra effort prioritizes fidelity and usually produces larger files. It is not a smaller-file mode.',fast:'Fast prioritizes speed, with lower fidelity and typically larger files than normal.'}[settings.effort];
    const dim=settings.width?`${settings.width}${settings.height?' × '+settings.height:''} px`:settings.height?`${settings.height} px high`:'Auto dimensions';
    $('recipeSummary').textContent=`${dim} · ${settings.fps} fps · Q${settings.quality}`;
    const rawDuration=metadata?.duration??metadata?.format?.duration;const duration=rawDuration!=null?Number(rawDuration):settings.source.kind==='sequence'&&metadata?.frameCount?metadata.frameCount/settings.fps:NaN;
    const start=Number(settings.trim.start)||0,end=settings.trim.end==null?duration:Number(settings.trim.end);
    if(sourceExists()&&Number.isFinite(end)&&end>start){const out=(end-start)/settings.speed*(settings.bounce?2:1);$('durationSummary').textContent=`≈ ${seconds(out)} · ${Math.ceil(out*settings.fps)} frames${settings.bounce?' · bounce':''}`;}
    else $('durationSummary').textContent=sourceExists()?'Ready to tune':'Load a clip to begin';
    $('discordBanner').hidden=!discordEnabled||currentTab!=='encode';
    const names={encode:discordEnabled?'Fit Discord · 20 MB':'Encode GIF',compare:'Run comparison',optimize:'Find a fit'};
    const adaptive=discordEnabled&&currentTab==='encode';
    $('estimateJob').hidden=adaptive;
    if(adaptive)$('recipeSummary').textContent='Adaptive · source matched · ≤20 MB';
    for(const node of $$('[data-key]'))if(['width','height','quality','motionQuality','lossyQuality','effort','fps'].includes(node.dataset.key))node.disabled=adaptive&&!(node.dataset.key==='fps'&&settings.source.kind==='sequence');
    $('estimateJob').disabled=!sourceExists();
    $('estimateJob').title=discordEnabled?'Estimates the current manual recipe; Discord finds its own final settings.':'Encode short samples to estimate size';
    $('runJob').replaceChildren(document.createTextNode(names[currentTab]||'Encode GIF'),el('span','', '↗'));
  }
  function renderPresetSelection(){$$('[data-preset]').forEach(node=>node.classList.toggle('selected',node.dataset.preset===selectedPreset));}
  function renderPresets(){
    $('presetGrid').replaceChildren(...builtinPresets.map(p=>{const b=button('', 'preset-card',()=>applyPreset(p));b.dataset.preset=p.id;b.append(el('span','preset-icon',p.icon),el('span','',p.name));b.title=p.description;return b;}));
    $('customPresetList').replaceChildren(...customPresets.map((p,index)=>{const row=el('div','custom-preset');row.append(button(p.name,'button subtle compact',()=>applyPreset(p)),button('×','button subtle compact',()=>{customPresets.splice(index,1);persistPresets();renderPresets();}));row.lastChild.setAttribute('aria-label',`Delete preset ${p.name}`);return row;}));renderPresetSelection();
  }
  function applyPreset(p){discordEnabled=p.id==='discord';sourceMatches.clear();recipeRevision++;if(lastEstimate)$('estimateStale').hidden=false;if(discordEnabled){selectedPreset='discord';switchTab('encode');syncFields();$('presetHint').textContent=p.description;schedulePlan();return;}const source=clone(settings.source),trim=clone(settings.trim),outputName=settings.outputName;settings={...clone(defaults),...clone(p.settings),source,trim,outputName};settings.crop={...defaults.crop,...p.settings.crop};settings.sweep={...defaults.sweep,...p.settings.sweep};settings.optimize={...defaults.optimize,...p.settings.optimize};selectedPreset=p.id||null;if(discordEnabled)switchTab('encode');syncFields();$('presetHint').textContent=p.description||`Saved recipe: ${p.name}`;schedulePlan();}
  function persistPresets(){try{localStorage.setItem('gifski-workbench-presets',JSON.stringify(customPresets));}catch{notice('Browser storage is full or unavailable. Export recipes to keep them.',true);}}
  function recipeForSave(){const recipe=clone(settings);delete recipe.source;delete recipe.trim;return recipe;}
  function validateImported(obj){
    if(!obj||typeof obj!=='object'||Array.isArray(obj))throw new Error('This file does not contain a recipe object.');
    const item=Object.prototype.hasOwnProperty.call(obj,'settings')?obj:{name:'Imported recipe',settings:obj};
    if(!item.settings||typeof item.settings!=='object'||Array.isArray(item.settings))throw new Error('Invalid recipe settings.');
    const clean={}, enums={effort:['normal','fast','extra'],pipeline:['auto','png','y4m','direct'],scaleFilter:['lanczos','bicubic','bilinear','neighbor','spline','area'],y4mColorOverride:[null,'','bt709','fcc','bt470bg','bt601','smpte240','ycgco']};
    const ranges={quality:[1,100,true],lossyQuality:[1,100,true,true],motionQuality:[1,100,true,true],width:[1,65535,true,true],height:[1,65535,true,true],fps:[0.001,100],speed:[0.001,1000],repeat:[-1,32767,true],denoise:[0,20],sharpen:[0,5],'crop.x':[0,65535,true],'crop.y':[0,65535,true],'crop.width':[1,65535,true],'crop.height':[1,65535,true],'optimize.targetMiB':[0.001,100000],'optimize.minQuality':[1,100,true],'optimize.maxQuality':[1,100,true],'optimize.minWidth':[1,65535,true],'optimize.minFps':[0.001,100],'optimize.maxTrials':[1,60,true]};
    function check(key,value,base){
      if(ranges[key]){const [min,max,integer,nullable]=ranges[key];if(value===null&&nullable)return value;if(typeof value!=='number'||!Number.isFinite(value)||value<min||value>max||(integer&&!Number.isInteger(value)))throw new Error(`Invalid recipe value for ${key}.`);return value;}
      if(enums[key]){if(!enums[key].includes(value))throw new Error(`Unknown recipe option for ${key}.`);return value||null;}
      if(key==='matte'){if(value!==null&&(typeof value!=='string'||!/^#?[a-f\d]{6}$/i.test(value)))throw new Error('Recipe matte must be null or a six-digit RGB color.');return value;}
      if(key==='fixedColors'){if(!Array.isArray(value)||value.length>255||value.some(c=>typeof c!=='string'||!/^#?[a-f\d]{6}$/i.test(c)))throw new Error('Recipe fixed colors must be an array of at most 255 RGB hex colors.');return value.slice();}
      if(key.startsWith('sweep.')){const max=key.endsWith('widths')?65535:100;if(!Array.isArray(value)||(!value.length&&!['sweep.motionQualities','sweep.lossyQualities'].includes(key))||value.length>60||value.some(v=>typeof v!=='number'||!Number.isFinite(v)||v<=0||v>max||(/qualities$/i.test(key)&&!Number.isInteger(v))))throw new Error(`Invalid recipe list for ${key}.`);return value.slice();}
      if(typeof base==='boolean'){if(typeof value!=='boolean')throw new Error(`Recipe ${key} must be true or false.`);return value;}
      if(typeof base==='string'){if(typeof value!=='string'||value.length>(key==='customFilter'?10000:180)||value.includes('\0'))throw new Error(`Invalid recipe text for ${key}.`);return value;}
      throw new Error(`Unsupported recipe value: ${key}`);
    }
    for(const key of Object.keys(defaults)){if(['source','trim'].includes(key)||!Object.prototype.hasOwnProperty.call(item.settings,key))continue;const value=item.settings[key];if(['crop','sweep','optimize'].includes(key)){if(!value||typeof value!=='object'||Array.isArray(value))throw new Error(`Invalid ${key} settings.`);clean[key]={};for(const subkey of Object.keys(defaults[key]))if(Object.prototype.hasOwnProperty.call(value,subkey))clean[key][subkey]=check(`${key}.${subkey}`,value[subkey],defaults[key][subkey]);}else clean[key]=check(key,value,defaults[key]);}
    const opt={...defaults.optimize,...clean.optimize};if(opt.minQuality>opt.maxQuality)throw new Error('Minimum quality cannot exceed maximum quality.');
    return {id:`custom-${Date.now()}`,name:String(item.name||'Imported recipe').slice(0,60),settings:clean};
  }
  function switchTab(tab){
    currentTab=tab;$$('[data-tab]').forEach(b=>b.classList.toggle('active',b.dataset.tab===tab));
    const work=['encode','compare','optimize'].includes(tab);$('controls').hidden=!work;$('actionbar').hidden=!work;$('appLayout').classList.toggle('wide-layout',!work);
    $('sourceSection').hidden=!work;$('encodeIntro').hidden=tab!=='encode';$('compareSection').hidden=tab!=='compare';$('optimizeSection').hidden=tab!=='optimize';$('queueSection').hidden=tab!=='queue';$('systemSection').hidden=tab!=='system';$('commandSection').hidden=!work;
    $('estimateSummary').hidden=!work||!lastEstimate;
    $('activeJob').hidden=!work||!currentJob;$('resultsSection').hidden=!work||!currentJob?.results?.length;
    updateRecipe();updateSourceMatches();if(tab==='queue')renderQueue();if(tab==='system')refreshStatus().catch(e=>notice(e.message,true));
  }
  function selectSourceTab(tab){$$('[data-source-tab]').forEach(b=>b.classList.toggle('active',b.dataset.sourceTab===tab));$('uploadArea').hidden=tab!=='upload';$('pathArea').hidden=tab!=='path';}
  async function loadSource(source,name){
    recipeRevision++;if(lastEstimate)$('estimateStale').hidden=false;settings.source=source;$('sourceBadge').textContent='READING SOURCE';$('sourceWarnings').textContent='';
    try{metadata=await api('/api/probe',{source});}catch(error){metadata=null;$('sourceWarnings').textContent=`Metadata unavailable: ${error.message}`;}
    $('sourceLoaded').hidden=false;$('uploadArea').hidden=true;$('pathArea').hidden=true;
    const sourcePath=metadata?.path||source.path||source.paths[0];const frameCount=metadata?.frameCount??source.paths.length;$('sourceName').textContent=name||(source.kind==='sequence'?`${frameCount} PNG frames`:metadata?.name||sourcePath.split(/[\\/]/).pop());$('sourcePathDisplay').textContent=source.kind==='sequence'?`${frameCount} PNG frames · ${source.path||source.paths[0]}`:sourcePath;
    $('sourceBadge').textContent=source.kind==='sequence'?`${frameCount} FRAMES`:'SOURCE READY';
    const url='/api/source?path='+encodeURIComponent(sourcePath);const image=/\.(png|gif|jpe?g|webp|bmp)$/i.test(sourcePath)||source.kind==='sequence';
    $('sourceVideo').pause();$('sourceVideo').removeAttribute('src');$('sourceVideo').load();$('sourceVideo').hidden=image;$('sourceImage').hidden=!image;$('previewFallback').hidden=true;
    if(image){$('sourceImage').src=url;}else{$('sourceVideo').src=url;$('sourceVideo').load();}
    const width=metadata?.width??metadata?.video?.width,height=metadata?.height??metadata?.video?.height;
    const fps=metadata?.fps??metadata?.frameRate??metadata?.frame_rate;
    const rows=[['DIMENSIONS',width&&height?`${width} × ${height}`:'—'],['DURATION',seconds(metadata?.duration??metadata?.format?.duration)],['SOURCE RATE',fps?`${Number(fps).toFixed(2)} fps`:'—'],['SOURCE SIZE',bytes(metadata?.size??metadata?.bytes??metadata?.format?.size)]];
    $('metadataGrid').replaceChildren(...rows.map(([label,value])=>{const n=el('div');n.append(el('span','',label),el('strong','',value));return n;}));
    if(width&&height){settings.crop.width=width;settings.crop.height=height;}
    for(const key of sourceMatches.keys()){const value=sourceValue(key);if(value!==null)set(settings,key,value);}
    syncFields();
    const warnings=metadata?.warnings;if(warnings)$('sourceWarnings').textContent=Array.isArray(warnings)?warnings.join(' '):String(warnings);
    if(source.kind==='sequence')$('sourceWarnings').textContent+=' Frame order is naturally sorted unless “Keep supplied PNG frame order” is enabled.';
    updateRecipe();await refreshPlan();
  }
  async function uploadFiles(files){
    const list=Array.from(files);if(!list.length)return;if(list.length>1&&!list.every(f=>/\.png$/i.test(f.name)))throw new Error('Select one video or a sequence containing only PNG frames.');
    $('uploadArea').setAttribute('aria-busy','true');const strong=$('uploadArea').querySelector('strong');const old=strong.textContent;const paths=[];
    try{for(let i=0;i<list.length;i++){strong.textContent=`Preparing ${i+1} of ${list.length}…`;const data=await api('/api/upload?name='+encodeURIComponent(list[i].name),undefined,{method:'POST',headers:{'Content-Type':'application/octet-stream'},body:list[i]});paths.push(data.path);}
      const sequence=list.length>1;await loadSource({kind:sequence?'sequence':'video',path:sequence?'':paths[0],paths:sequence?paths:[]},sequence?`${list.length} PNG frames`:list[0].name);
    }finally{strong.textContent=old;$('uploadArea').removeAttribute('aria-busy');$('fileInput').value='';}
  }
  function sourceError(){if(!sourceExists())return;$('sourceVideo').hidden=true;$('sourceImage').hidden=true;$('previewFallback').hidden=false;}
  async function refreshStatus(){
    const request=++statusRequest;const nextState=await api('/api/status');if(request!==statusRequest)return;toolsState=nextState;const tools=toolsState.tools||{};const missing=Object.entries(tools).filter(([,v])=>!v.available).map(([k])=>k);
    const status=$('engineStatus');status.textContent=missing.length?`Missing: ${missing.join(', ')} · see System`:'Local engines ready';status.classList.toggle('ready',!missing.length);status.classList.toggle('missing',!!missing.length);
    for(const name of ['gifski','ffmpeg','ffprobe']){
      const tool=tools[name]||{};let row=$('tool-row-'+name),input=$('path-'+name);
      if(!row){
        row=el('div','tool-field');row.id='tool-row-'+name;
        const head=el('div','tool-field-head');head.append(el('strong','',name),el('span','tool-state'));
        const label=el('label','',`${name} executable`);input=el('input');input.type='text';input.id='path-'+name;
        input.placeholder=name==='gifski'?'C:/Tools/gifski.exe':`C:/ffmpeg/bin/${name}.exe`;
        input.addEventListener('input',()=>toolDrafts.set(name,input.value));
        label.append(input);row.append(head,label,el('p','tool-version'));$('toolFields').append(row);
      }
      const badge=row.querySelector('.tool-state');badge.textContent=tool.available?'● Available':'○ Not found';badge.classList.toggle('missing',!tool.available);
      if(!toolDrafts.has(name))input.value=toolsState.config?.[name]||'';
      row.querySelector('.tool-version').textContent=tool.version||tool.error||tool.path||'Add an executable path or place the tool on PATH.';
    }
    $('toolHelp').replaceChildren(...['gifski','ffmpeg','ffprobe'].map(name=>{const panel=el('details','panel tool-help');panel.append(el('summary','',`${name} · installed version & CLI help`),el('pre','code-block',[tools[name]?.version,tools[name]?.help].filter(Boolean).join('\n\n')||'No help output available. Check the executable path above.'));return panel;}));
  }
  function planText(plan){if(typeof plan==='string')return plan;if(Array.isArray(plan)){if(plan.every(v=>typeof v==='string'||typeof v==='number'))return plan.map(v=>/\s/.test(String(v))?JSON.stringify(v):String(v)).join(' ');return plan.map(planText).join('\n\n');}if(!plan)return '';if(plan.display)return plan.display;if(plan.argv)return plan.argv.map(v=>/\s/.test(String(v))?JSON.stringify(v):String(v)).join(' ');if(plan.commands)return planText(plan.commands);return JSON.stringify(plan,null,2);}
  async function refreshPlan(){if(!sourceExists()){$('commandOutput').textContent='Load a source to inspect the encoding plan.';return;}try{const plan=await api('/api/plan',{settings});$('commandOutput').textContent=planText(plan);const notes=[...(plan.warnings||[]),...(plan.notes||[])];if(notes.length)$('commandOutput').textContent+='\n\nNotes:\n'+notes.join('\n');}catch(error){$('commandOutput').textContent='Plan unavailable: '+error.message;}}
  function schedulePlan(){clearTimeout(planTimer);if($('commandSection').open)planTimer=setTimeout(refreshPlan,500);}
  function parseList(id){const values=$(id).value.split(/[\s,;]+/).filter(Boolean).map(Number);if(!values.length||values.some(v=>!Number.isFinite(v)||v<=0))throw new Error('Enter comma-separated positive numbers for each comparison axis.');return [...new Set(values)];}
  function updateTrialCount(){try{const count=parseList('sweepQualities').length*parseList('sweepWidths').length*parseList('sweepFps').length*($('sweepMotion').value.trim()?parseList('sweepMotion').length:1)*($('sweepLossy').value.trim()?parseList('sweepLossy').length:1);$('trialCount').textContent=`${count} complete encode${count===1?'':'s'}`;}catch{$('trialCount').textContent='Enter valid trial values';}}
  function captureModeSettings(){if(currentTab==='compare')settings.sweep={qualities:parseList('sweepQualities'),widths:parseList('sweepWidths'),fps:parseList('sweepFps'),motionQualities:$('sweepMotion').value.trim()?parseList('sweepMotion'):[],lossyQualities:$('sweepLossy').value.trim()?parseList('sweepLossy'):[]};for(const key of ['targetMiB','minQuality','maxQuality','minWidth','minFps','maxTrials'])settings.optimize[key]=Number($(key).value);for(const key of ['allowResize','allowFps'])settings.optimize[key]=$(key).checked;}
  function validateSettings(){
    if(!sourceExists())throw new Error('Choose a source file first.');
    for(const n of $$('[data-key]'))if(!n.checkValidity())throw new Error(`Check the ${n.dataset.key} field.`);
    if(settings.trim.end!==null&&settings.trim.end<=settings.trim.start)throw new Error('The trim end must be later than the start.');
    if(settings.fixedColors.some(c=>!/^#?[a-f\d]{6}$/i.test(c)))throw new Error('Fixed palette colors must contain exactly six hexadecimal digits, for example FF7957.');
    if(settings.fixedColors.length>255)throw new Error('Gifski accepts at most 255 fixed palette colors.');
    if(settings.matte&&!/^#?[a-f\d]{6}$/i.test(settings.matte))throw new Error('Matte color must contain exactly six hexadecimal digits.');
    if(settings.source.kind==='sequence'&&settings.fps*settings.speed>100)throw new Error('For a PNG sequence, frame rate × speed must not exceed 100 fps.');
    if(settings.pipeline==='y4m'&&settings.bounce)throw new Error('Bounce requires the Automatic or PNG pipeline.');
    if(currentTab==='compare'){
      if(settings.sweep.qualities.some(q=>q>100||!Number.isInteger(q)))throw new Error('Comparison qualities must be whole numbers from 1 to 100.');
      if(settings.sweep.widths.some(w=>w>65535||!Number.isInteger(w)))throw new Error('Comparison widths must be whole pixels from 1 to 65,535.');
      if(settings.sweep.fps.some(f=>f>100))throw new Error('Comparison frame rates must not exceed 100 fps.');
    }
    if(currentTab==='optimize'){
      for(const key of ['targetMiB','minQuality','maxQuality','minWidth','minFps','maxTrials'])if(!$(key).checkValidity()||!Number.isFinite(settings.optimize[key]))throw new Error(`Check the target-size ${key} field.`);
      if(settings.optimize.minQuality>settings.optimize.maxQuality)throw new Error('Minimum quality cannot exceed maximum quality.');
      if(settings.optimize.allowResize&&settings.width&&settings.optimize.minWidth>settings.width)throw new Error('Minimum width cannot exceed the current output width.');
      if(settings.optimize.allowFps&&settings.optimize.minFps>settings.fps)throw new Error('Minimum frame rate cannot exceed the current output frame rate.');
    }
  }
  async function runJob(requestedMode){captureModeSettings();validateSettings();const mode=typeof requestedMode==='string'?requestedMode:currentTab==='compare'?'sweep':currentTab==='optimize'?'optimize':discordEnabled?'discord':'encode';if(mode==='estimate')estimateRevision=recipeRevision;$('runJob').disabled=true;$('estimateJob').disabled=true;try{const data=await api('/api/jobs',{settings:clone(settings),mode});currentJob=data.job||data;pins={A:null,B:null};renderJob();await pollJobs();notice(mode==='encode'?'Encoding job added.':'Trial job added. You can keep tuning while it runs.');$('activeJob').scrollIntoView({behavior:'smooth',block:'nearest'});}finally{$('runJob').disabled=false;$('estimateJob').disabled=!sourceExists();}}
  function activeStatus(status){return ['queued','running','preparing','encoding','pending'].includes(status);}
  async function pollJobs(){if(pollBusy)return;pollBusy=true;try{const data=await api('/api/jobs');jobs=Array.isArray(data)?data:data.jobs||[];$('queueCount').textContent=jobs.length;if(currentJob){const latest=jobs.find(j=>j.id===currentJob.id);currentJob=latest||await api('/api/jobs/'+encodeURIComponent(currentJob.id));renderJob();}if(currentTab==='queue')renderQueue();}catch(error){if(!toolsState)notice('Cannot reach the local server. Keep the launcher window open.',true);}finally{pollBusy=false;}}
  function renderJob(){
    if(!currentJob)return;const job=currentJob;const work=['encode','compare','optimize'].includes(currentTab);$('activeJob').hidden=!work;$('jobMode').textContent=({encode:'SINGLE ENCODE',sweep:'COMPARISON LAB',optimize:'TARGET-SIZE SEARCH',estimate:'SIZE ESTIMATION',discord:'DISCORD · 20 MB SEARCH'})[job.mode]||'ENCODING JOB';$('jobStage').textContent=job.stage||job.status||'Queued';$('jobStatus').textContent=String(job.status||'queued').replaceAll('_',' ');$('cancelJob').hidden=!activeStatus(job.status);
    const progress=Number(job.progress);const known=job.progress!==null&&job.progress!==undefined&&Number.isFinite(progress);$('jobProgress').classList.toggle('indeterminate',!known&&activeStatus(job.status));$('jobProgress').style.width=(known?Math.max(0,Math.min(100,progress)):job.status==='completed'?100:0)+'%';$('jobPercent').textContent=known?`${Math.round(progress)}%`:activeStatus(job.status)?'Working…':'';
    $('jobError').hidden=!job.error;$('jobError').textContent=job.error||'';$('jobWarnings').hidden=!job.warnings?.length;$('jobWarnings').textContent=(job.warnings||[]).join(' ');const logs=Array.isArray(job.logs)?job.logs.join('\n'):String(job.logs||'');if($('jobLogs').textContent!==logs){$('jobLogs').textContent=logs;$('jobLogs').scrollTop=$('jobLogs').scrollHeight;}
    if(job.estimate){lastEstimate=job.estimate;renderEstimate(job.estimate);}
    renderResults(job);
  }
  function resultURL(result,job,download=false){let url=download?result.downloadUrl:result.previewUrl;url=url||`/api/files/${encodeURIComponent(job.id)}/${result.index??job.results.indexOf(result)}`;if(download&&!/[?&]download=/.test(url))url+=(url.includes('?')?'&':'?')+'download=1';return url;}
  function allResults(job){return (job.results||[]).map((result,i)=>({...result,index:result.index??i}));}
  function candidateKey(job,result){return `${job.id}:${result.index}:${result.bytes}`;}
  let resultSignature='';
  function renderResults(job){
    const results=allResults(job);$('resultsSection').hidden=!results.length||!['encode','compare','optimize'].includes(currentTab);if(!results.length)return;
    const signature=job.id+'|'+job.status+'|'+job.bestIndex+'|'+results.map(r=>`${r.index}:${r.bytes}:${r.fit}`).join(',');if(resultSignature===signature)return;resultSignature=signature;
    $('resultCount').textContent=`${results.length} CANDIDATE${results.length===1?'':'S'}`;
    const budget=Number(job.targetBytes)||Number(job.settings?.optimize?.targetMiB)*1048576;const fit=results.filter(r=>r.fit===true||(['optimize','discord'].includes(job.mode)&&r.bytes<=budget));
    $('resultSummary').textContent=job.mode==='estimate'?(job.estimate?.exact?'Full trim range encoded for an exact measurement.':'Sample encodes used for the estimate. Sample downloads contain only the tested intervals.'): ['optimize','discord'].includes(job.mode)?`${fit.length} of ${results.length} candidates fit ${job.mode==='discord'?'20 MB (20,000,000 bytes)':bytes(budget)}. ${activeStatus(job.status)?'Search is still running.':'Review detail and motion before choosing your download.'}`:`Measured outputs from the same source. Pin A and B to compare; GIF playback is approximate, not frame-locked.`;
    if(job.searchNote)$('resultSummary').textContent+=' '+job.searchNote;
    $('resultsGrid').replaceChildren(...results.map(r=>{
      const best=r.index===job.bestIndex||r.best===true;const card=el('article','candidate'+(best?' best':''));const stage=el('div','candidate-image');const img=el('img');img.src=resultURL(r,job);img.alt=`Candidate ${r.index+1}, quality ${r.quality??r.settings?.quality??'unknown'}`;img.loading='lazy';stage.append(img);
      const body=el('div','candidate-body'),title=el('div','candidate-title');title.append(el('strong','',bytes(r.bytes)),el('span','',r.sample?'SAMPLE':best?(r.fit===false?'SMALLEST FOUND':'BEST FOUND'):r.fit===true?'FITS BUDGET':''));
      const width=r.width??r.settings?.width??'auto',height=r.height??r.settings?.height;body.append(title,el('div','candidate-info',`${width}${height?' × '+height:' px'} · ${r.fps??r.settings?.fps??'—'} fps · Q${r.quality??r.settings?.quality??'—'}`));
      if(['optimize','discord'].includes(job.mode)&&budget>0){const track=el('div','budget-track'),fill=el('div',r.bytes>budget?'over':'');fill.style.width=Math.min(100,r.bytes/budget*100)+'%';track.title=`${(r.bytes/budget*100).toFixed(1)}% of budget`;track.append(fill);body.append(track);}
      const actions=el('div','candidate-actions');const download=el('a','button secondary',r.sample?'Download sample':'Download');download.href=resultURL(r,job,true);download.download=r.filename||'animation.gif';actions.append(download,button('Use recipe','button subtle',()=>{const source=clone(settings.source);const next=clone(r.settings||job.settings||{});settings={...clone(defaults),...next,source};if(r.sample)settings.trim=clone(job.settings.trim);discordEnabled=false;sourceMatches.clear();recipeRevision++;syncFields();selectedPreset=null;renderPresetSelection();notice('Candidate recipe applied to the controls.');schedulePlan();}));
      const pinActions=el('div','pin-actions');for(const slot of ['A','B']){const b=button(`Pin ${slot}`,'text-button',()=>{pins[slot]={job:clone(job),result:clone(r)};renderComparison();renderPins();});b.dataset.pin=slot;b.dataset.candidate=candidateKey(job,r);pinActions.append(b);}body.append(actions,pinActions);card.append(stage,body);return card;
    }));renderChart(results,job);renderPins();renderComparison();
  }
  function renderPins(){$$('[data-pin]').forEach(b=>b.classList.toggle('active',pins[b.dataset.pin]&&candidateKey(pins[b.dataset.pin].job,pins[b.dataset.pin].result)===b.dataset.candidate));}
  function renderComparison(){const pane=$('comparisonPanes');pane.hidden=!pins.A&&!pins.B;pane.classList.toggle('actual',actualPixels);pane.replaceChildren(...['A','B'].map(slot=>{const pin=pins[slot],card=el('div','comparison-pane'),head=el('div','comparison-pane-header');head.append(el('b','',slot),el('span','',pin?`${bytes(pin.result.bytes)} · ${pin.result.width??pin.result.settings?.width??'auto'} px · ${pin.result.fps??pin.result.settings?.fps} fps · Q${pin.result.quality??pin.result.settings?.quality}`:'Choose a candidate below'));const media=el('div','comparison-pane-media');if(pin){const img=el('img');img.alt=`Comparison ${slot}`;img.src=resultURL(pin.result,pin.job);media.append(img);}else media.append(el('span','hint','Pin a candidate to this slot'));card.append(head,media);return card;}));}
  function renderChart(results,job){const chart=$('resultChart');$('chartPanel').hidden=results.length<2;if(results.length<2)return;const ns='http://www.w3.org/2000/svg';const svg=(tag,attrs,text)=>{const n=document.createElementNS(ns,tag);for(const [k,v]of Object.entries(attrs))n.setAttribute(k,v);if(text!==undefined)n.textContent=text;return n;};chart.replaceChildren();const left=66,right=735,top=15,bottom=155;const ys=results.map(r=>Number(r.bytes)||0);const budget=['optimize','discord'].includes(job.mode)?Number(job.targetBytes)||Number(job.settings?.optimize?.targetMiB)*1048576:0;const max=Math.max(...ys,budget,1)*1.1;
    for(let i=0;i<=3;i++){const y=bottom-(bottom-top)*i/3;chart.append(svg('line',{x1:left,x2:right,y1:y,y2:y,stroke:'#303641'}),svg('text',{x:left-10,y:y+3,'text-anchor':'end'},bytes(max*i/3)));}
    for(const q of [0,25,50,75,100])chart.append(svg('text',{x:left+(right-left)*q/100,y:178,'text-anchor':'middle'},String(q)));
    if(budget){const y=bottom-budget/max*(bottom-top);chart.append(svg('line',{x1:left,x2:right,y1:y,y2:y,stroke:'#84c8ab','stroke-dasharray':'5 5'}),svg('text',{x:right,y:y-6,'text-anchor':'end'},'Budget'));}
    for(const r of results){const q=Number(r.quality??r.settings?.quality??job.settings?.quality);const point=svg('circle',{class:'point',cx:left+(right-left)*q/100,cy:bottom-r.bytes/max*(bottom-top),r:5});point.append(svg('title',{},`Candidate ${r.index+1}: ${bytes(r.bytes)}, quality ${q}, ${r.width??r.settings?.width} px, ${r.fps??r.settings?.fps} fps`));chart.append(point);}
  }
  function renderQueue(){if(!jobs.length){$('queueList').replaceChildren(el('div','queue-empty','Your first encode will appear here. Start with Create GIF, Compare trials, or Fit a size.'));return;}$('queueList').replaceChildren(...jobs.slice().reverse().map(job=>{const card=el('article','queue-card'),content=el('div');const source=job.settings?.source;const name=(source?.path||source?.paths?.[0]||job.id).split(/[\\/]/).pop();content.append(el('span','queue-status',job.status),el('h3','',name),el('p','',`${job.mode||'encode'} · ${job.results?.length||0} outputs · ${job.stage||''}`));const actions=el('div','queue-actions');actions.append(button('Open results','button secondary compact',()=>{currentJob=job;if(job.mode==='estimate')estimateRevision=-1;resultSignature='';switchTab(job.mode==='sweep'?'compare':job.mode==='optimize'?'optimize':'encode');renderJob();}));if(activeStatus(job.status))actions.append(button('Cancel','button subtle compact',safeRun(async()=>{await api('/api/jobs/'+encodeURIComponent(job.id)+'/cancel',{});await pollJobs();})));card.append(content,actions);return card;}));}
  function sourceValue(key){
    if(!metadata)return null;
    const raw=key==='trim.end'?metadata.duration:key==='fps'?(settings.source.kind==='sequence'?null:metadata.fps):metadata[key];
    const value=Number(raw);if(raw==null||!Number.isFinite(value)||value<=0)return null;
    return key==='fps'?Math.round(Math.min(50,value)*1000)/1000:key==='width'||key==='height'?Math.round(value):Math.round(value*100)/100;
  }
  function updateSourceMatches(){
    $$('[data-match]').forEach(b=>{const key=b.dataset.match;const value=sourceValue(key);b.disabled=value===null||(discordEnabled&&currentTab==='encode'&&key!=='trim.end');b.setAttribute('aria-pressed',String(sourceMatches.has(key)));b.title=value===null?'Load a source with this metadata first.':`Match source: ${key==='fps'?value.toFixed(3)+' fps':key==='trim.end'?seconds(value):value+' px'}${key==='fps'&&metadata.fps>50?' (capped at 50 fps for GIF)':''}`;});
  }
  function toggleSourceMatch(key){
    if(sourceMatches.has(key)){set(settings,key,sourceMatches.get(key));sourceMatches.delete(key);}
    else{const value=sourceValue(key);if(value===null)return;sourceMatches.set(key,get(settings,key));set(settings,key,value);
      if(key==='width'||key==='height'){const other=key==='width'?'height':'width';sourceMatches.delete(other);set(settings,other,null);}
    }
    changed();syncFields();
  }
  function renderEstimate(estimate){
    $('estimateSummary').hidden=!['encode','compare','optimize'].includes(currentTab);
    $('estimateLabel').textContent=estimate.exact?'MEASURED FULL OUTPUT SIZE':'SAMPLED SIZE ESTIMATE';
    $('estimateValue').textContent=bytes(estimate.estimatedBytes);
    $('estimateRange').textContent=estimate.exact?'Full trim range encoded.':`${bytes(estimate.lowerBytes)} – ${bytes(estimate.upperBytes)} approximate range · ${estimate.sampleCount} samples`;
    $('estimateNote').textContent=estimate.note||'Sample-based estimate. Scene changes can alter the final file size; a full encode is required to verify a limit.';
    $('estimateStale').hidden=estimateRevision===recipeRevision;
  }
  function allToolsReady(){return ['gifski','ffmpeg','ffprobe'].every(name=>toolsState?.tools?.[name]?.available);}
  async function refreshDependencies(){
    if(dependencyBusy)return;dependencyBusy=true;
    try{dependencyState=await api('/api/dependencies');renderDependencies();if(dependencyState.busy){if(!dependencyPoll)dependencyPoll=setInterval(()=>refreshDependencies().catch(showSetupError),1200);}else if(dependencyPoll||Object.values(dependencyState.tools||{}).some(t=>t.state==='completed')){clearInterval(dependencyPoll);dependencyPoll=null;await refreshStatus();renderDependencies();}}
    finally{dependencyBusy=false;}
  }
  function showSetupError(error){$('setupError').hidden=false;$('setupError').textContent=error.message||String(error);}
  function renderDependencies(){
    const state=dependencyState;if(!state)return;
    $('setupPlatform').textContent=`${state.platform||'This system'} · ${state.supported?'Portable installation available.':'Automatic installation is not available on this platform. Use the commands below or existing executable paths.'}`;
    $('dependencyCards').replaceChildren(...['gifski','ffmpeg'].map(name=>{
      const item=state.tools?.[name]||{},ready=!!toolsState?.tools?.[name]?.available&&(name!=='ffmpeg'||!!toolsState?.tools?.ffprobe?.available),installing=item.state==='installing';
      const card=el('div','dependency-card'),heading=el('div','dependency-heading');heading.append(el('strong','',name==='ffmpeg'?'FFmpeg + FFprobe':'Gifski'),el('span','tool-state'+(ready?'':' missing'),ready?'Ready':installing?'Installing…':item.state==='failed'?'Installation failed':'Missing'));card.append(heading);
      const install=button(ready?'Installed':installing?'Installing…':'Install','button secondary compact',safeRun(async()=>{install.disabled=true;$('setupError').hidden=true;try{dependencyState=await api('/api/dependencies/install',{tool:name});renderDependencies();await refreshDependencies();}catch(error){showSetupError(error);await refreshDependencies();}}));install.disabled=ready||!!state.busy||!state.supported;card.append(install);
      if(item.message)card.append(el('p','hint',item.message));if(item.error)card.append(el('p','error-text',item.error));
      if(installing){const meter=el('progress');meter.max=100;meter.value=Math.max(0,Math.min(100,Number(item.progress)||0));meter.setAttribute('aria-label',`${name} installation progress`);card.append(meter);}
      if(!state.supported&&state.manual?.[name])card.append(el('pre','code-block',typeof state.manual[name]==='string'?state.manual[name]:JSON.stringify(state.manual[name],null,2)));
      return card;
    }));updateSetup();
  }
  function updateSetup(){
    $$('[data-setup-panel]').forEach(n=>n.hidden=Number(n.dataset.setupPanel)!==setupStep);$$('[data-setup-step]').forEach(n=>n.classList.toggle('active',Number(n.dataset.setupStep)===setupStep));
    $('setupBack').hidden=setupStep===0;$('setupNext').textContent=setupStep===2?'Open workspace':'Continue';$('setupNext').disabled=setupStep===0&&!allToolsReady();
    $('setupReadyNote').textContent=allToolsReady()?'Gifski, FFmpeg, and FFprobe are available.':'You can finish setup later from System → Guided setup.';
  }
  async function openSetup(){setupStep=0;$('setupError').hidden=true;updateSetup();if(!$('setupDialog').open)$('setupDialog').showModal();try{await refreshStatus();await refreshDependencies();updateSetup();}catch(error){showSetupError(error);}}
  async function checkSetup(){if(setupChecked)return;setupChecked=true;if(!allToolsReady())await openSetup();}
  $$('[data-match]').forEach(b=>b.addEventListener('click',event=>{event.preventDefault();toggleSourceMatch(b.dataset.match);}));
  $('exitDiscord').addEventListener('click',()=>{discordEnabled=false;selectedPreset=null;updateRecipe();updateSourceMatches();renderPresetSelection();});
  $('estimateJob').addEventListener('click',safeRun(()=>runJob('estimate')));
  $('openSetup').addEventListener('click',safeRun(openSetup));
  $('closeSetup').addEventListener('click',()=>$('setupDialog').close());
  $('setupBack').addEventListener('click',()=>{setupStep=Math.max(0,setupStep-1);updateSetup();});
  $('setupNext').addEventListener('click',()=>{if(setupStep===0&&!allToolsReady())return;if(setupStep<2){setupStep++;updateSetup();return;}const choice=$$('input[name="setupWorkflow"]').find(n=>n.checked)?.value||'balanced';applyPreset(builtinPresets.find(p=>p.id===choice));$('setupDialog').close();switchTab('encode');});
  $('setupManual').addEventListener('click',()=>{$('setupDialog').close();switchTab('system');});
  $('setupRecheck').addEventListener('click',safeRun(async()=>{await refreshStatus();await refreshDependencies();updateSetup();}));
  $$('[data-key]').forEach(node=>node.addEventListener(node.type==='checkbox'||node.tagName==='SELECT'?'change':'input',()=>{let value=node.type==='checkbox'?node.checked:node.type==='number'||node.type==='range'?Number(node.value):node.value;if(node.dataset.null&&node.value==='')value=null;sourceMatches.delete(node.dataset.key);set(settings,node.dataset.key,value);changed();}));
  $$('[data-tab]').forEach(b=>b.addEventListener('click',()=>switchTab(b.dataset.tab)));
  $$('[data-source-tab]').forEach(b=>b.addEventListener('click',()=>selectSourceTab(b.dataset.sourceTab)));
  $('systemShortcut').addEventListener('click',()=>switchTab('system'));
  $('dismissNotice').addEventListener('click',()=>{$('notice').hidden=true;});
  $('resetSettings').addEventListener('click',()=>{const source=settings.source;discordEnabled=false;sourceMatches.clear();recipeRevision++;settings=clone(defaults);settings.source=source;selectedPreset='balanced';syncFields();schedulePlan();notice('Encoding settings reset.');});
  $('fixedColors').addEventListener('input',()=>{settings.fixedColors=$('fixedColors').value.split(/[\s,;]+/).filter(Boolean);changed();});
  $('loopMode').addEventListener('change',()=>{settings.repeat=$('loopMode').value==='once'?-1:$('loopMode').value==='forever'?0:1;$('repeatField').hidden=settings.repeat<=0;$$('[data-key="repeat"]')[0].disabled=settings.repeat<=0;$$('[data-key="repeat"]')[0].value=settings.repeat;changed();});
  $('savePreset').addEventListener('click',safeRun(()=>{captureModeSettings();$('presetName').value='';$('presetDialog').showModal();}));
  $('confirmPreset').addEventListener('click',event=>{if(!$('presetName').value.trim()){event.preventDefault();return;}const name=$('presetName').value.trim();const existing=customPresets.findIndex(p=>p.name===name);const item={id:`custom-${Date.now()}`,name,settings:recipeForSave()};if(existing>=0)customPresets[existing]=item;else customPresets.push(item);persistPresets();renderPresets();notice(`Saved “${name}”.`);});
  $('exportPreset').addEventListener('click',safeRun(()=>{captureModeSettings();const blob=new Blob([JSON.stringify({format:'gifski-workbench-recipe',version:1,name:'My Gifski recipe',settings:recipeForSave()},null,2)],{type:'application/json'});const url=URL.createObjectURL(blob),a=el('a');a.href=url;a.download='gifski-recipe.json';a.click();setTimeout(()=>URL.revokeObjectURL(url),1000);}));
  $('importPreset').addEventListener('change',safeRun(async()=>{const file=$('importPreset').files[0];if(!file)return;if(file.size>100000)throw new Error('Recipe files must be smaller than 100 KB.');const p=validateImported(JSON.parse(await file.text()));customPresets.push(p);persistPresets();applyPreset(p);$('importPreset').value='';notice('Recipe imported. Review settings before encoding.');}));
  $('fileInput').addEventListener('change',safeRun(e=>uploadFiles(e.target.files)));
  $('uploadArea').addEventListener('click',()=>$('fileInput').click());$('uploadArea').addEventListener('keydown',e=>{if(e.key==='Enter'||e.key===' '){e.preventDefault();$('fileInput').click();}});
  for(const name of ['dragenter','dragover'])$('uploadArea').addEventListener(name,e=>{e.preventDefault();$('uploadArea').classList.add('dragover');});
  for(const name of ['dragleave','drop'])$('uploadArea').addEventListener(name,e=>{e.preventDefault();$('uploadArea').classList.remove('dragover');});
  $('uploadArea').addEventListener('drop',safeRun(e=>uploadFiles(e.dataTransfer.files)));
  $('sourceKind').addEventListener('change',()=>{const sequence=$('sourceKind').value==='sequence';$('singlePathLabel').hidden=sequence;$('sequencePathLabel').hidden=!sequence;});
  $('loadPath').addEventListener('click',safeRun(async()=>{const kind=$('sourceKind').value;const path=$('sourcePath').value.trim().replace(/^"|"$/g,'');const paths=$('sourcePaths').value.split('\n').map(s=>s.trim().replace(/^"|"$/g,'')).filter(Boolean);if(kind==='sequence'?!paths.length:!path)throw new Error('Enter a source path first.');const directory=kind==='sequence'&&paths.length===1&&!/\.png$/i.test(paths[0]);await loadSource({kind,path:directory?paths[0]:kind==='sequence'?'':path,paths:kind==='sequence'&&!directory?paths:[]});}));
  $('changeSource').addEventListener('click',()=>{$('sourceLoaded').hidden=true;selectSourceTab('upload');});
  $('sourceVideo').addEventListener('error',sourceError);$('sourceImage').addEventListener('error',sourceError);
  $('markStart').addEventListener('click',()=>{settings.trim.start=Math.round(($('sourceVideo').currentTime||0)*100)/100;syncFields();changed();});
  $('markEnd').addEventListener('click',()=>{const t=$('sourceVideo').currentTime;if(!t){notice('Move the video playhead to the desired end first.');return;}sourceMatches.delete('trim.end');settings.trim.end=Math.round(t*100)/100;syncFields();changed();});
  $('saveTools').addEventListener('click',safeRun(async()=>{
    const config={},submitted={};for(const name of ['gifski','ffmpeg','ffprobe']){submitted[name]=$('path-'+name).value;config[name]=submitted[name].trim().replace(/^"|"$/g,'');}
    $('saveTools').disabled=true;statusRequest++;
    try{await api('/api/config',config);for(const name of Object.keys(submitted))if(toolDrafts.get(name)===submitted[name])toolDrafts.delete(name);await refreshStatus();notice(toolDrafts.size?'Saved. Newer edits remain unsaved.':'Executable paths saved and checked.');}
    finally{$('saveTools').disabled=false;}
  }));
  $('refreshTools').addEventListener('click',safeRun(refreshStatus));$('refreshPlan').addEventListener('click',refreshPlan);$('commandSection').addEventListener('toggle',()=>{if($('commandSection').open)refreshPlan();});
  $('copyPlan').addEventListener('click',safeRun(async()=>{await navigator.clipboard.writeText($('commandOutput').textContent);notice('Command plan copied.');}));
  for(const id of ['sweepQualities','sweepWidths','sweepFps','sweepMotion','sweepLossy'])$(id).addEventListener('input',updateTrialCount);
  $('runJob').addEventListener('click',safeRun(runJob));$('cancelJob').addEventListener('click',safeRun(async()=>{if(currentJob){await api('/api/jobs/'+encodeURIComponent(currentJob.id)+'/cancel',{});await pollJobs();}}));
  $('syncCompare').addEventListener('click',()=>{$$('#comparisonPanes img').forEach(img=>{const url=new URL(img.src,location.href);url.searchParams.set('restart',Date.now());img.src=url.href;});notice('GIF previews restarted. Browser playback is not frame-locked.');});
  $('toggleZoom').addEventListener('click',()=>{actualPixels=!actualPixels;$('comparisonPanes').classList.toggle('actual',actualPixels);$('toggleZoom').textContent=actualPixels?'Fit previews':'Show actual pixels';});
  syncFields();refreshStatus().then(checkSetup).catch(e=>{notice(e.message,true);$('engineStatus').textContent='Local server unavailable';});pollJobs();setInterval(pollJobs,1600);
})();
