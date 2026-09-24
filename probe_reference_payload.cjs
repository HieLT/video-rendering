const fs=require('fs'),vm=require('vm'),assert=require('assert'),path=require('path');
const root=path.join(__dirname,'diagnostics/reference_capabilities');
const modules={};const self={__LOADABLE_LOADED_CHUNKS__:{push:c=>Object.assign(modules,c[1])}};
for(const file of ['bundle_325.js','bundle_311.js','bundle_63.js'])vm.runInNewContext(fs.readFileSync(path.join(root,file),'utf8'),{self});
const enumMap=new Proxy({}, {get:(_,key)=>key});
const values={'video-model':'seedance_v2.5','video-duration':'10','video-ratio':'16:9'};
const stubs={
 29065:{$:x=>x},291046:{Z:()=> 'synthetic-block'},133869:{default:(o,k)=>o?.[k]},
 619122:new Proxy({Or:x=>x.type==='image'},{get:(o,k)=>o[k]||(()=>false)}),
 599838:{MZ:{ParseState_Default:0},m9:{ReviewState_Access:1}},
 910389:{sf:{AttachmentTypeImage:1},cy:{UploadStatusSuccess:1}},
 284662:{zcD:{BLOCK_ATTACHMENT:1010}},460381:{LD:{0:0},xo:{1:1}},
 531047:{d:()=>{}},480725:{l_:()=>({data:[]}),T7:(_,k)=>values[k],YC:k=>values[k]},
 612794:{NS:()=>({getState:()=>({inputOptionsMap:{}})})},
 573018:{Fd:()=>undefined,wF:()=>undefined,D$:({userPrompt})=>userPrompt},
 102704:{Q:()=>undefined},675966:{SkillType:{VideoGeneration:17}},
 320509:{lx:()=> 'video'},448213:{Ux:{UPLOAD_FROM_PLUS_BUTTON:'plus'}},
 439783:{iI:()=>false},449213:{X:key=>key==='skill_video_message_prefix_global'?'Generated video: ':key},
 498760:{Wp:()=>({getState:()=>({preprocessTaskMap:{}})})},
};
const cache={};function req(id){if(id in stubs)return stubs[id];if(cache[id])return cache[id].exports;if(![120597,587594,339259].includes(id))return {};const m={exports:{}};cache[id]=m;modules[id](m,m.exports,req);return m.exports;}
req.d=(target,defs)=>{for(const [k,get]of Object.entries(defs))Object.defineProperty(target,k,{get,enumerable:true});};req.r=()=>{};
(async()=>{
 const attachments=[{type:'image',localKey:'local-red',role:'first_frame',alias:'HERO'},{type:'image',localKey:'local-blue',role:'last_frame',alias:'END'}];
 const attachmentStates=attachments.map((a,i)=>({...a,fileName:i?'reference_blue.png':'reference_red.png',imageWidth:512,imageHeight:512,url:'https://example.invalid/'+i+'.png',extraParams:{role:a.role,reference_name:a.alias},uploadPercent:100}));
 const block=req(120597).S({attachments,attachmentStates,attachmentKeys:['synthetic/red.png','synthetic/blue.png']});
 const images=block.content.attachment_block.attachments;
 assert.equal(images.length,2);assert.equal(images[0].identifier,'local-red');assert.equal(images[1].identifier,'local-blue');assert.equal(images[0].image.name,'reference_red.png');
 assert(!JSON.stringify(images).includes('first_frame'));assert(!JSON.stringify(images).includes('last_frame'));assert(!JSON.stringify(images).includes('HERO'));
 const node=req(339259).M.submitNodes.find(n=>n.id==='video-creation:input-skill-node');
 const context={content:{text:'@HERO walks toward @END',editorValue:{data:[]},attachmentStates,attachments,attachmentKeys:['synthetic/red.png','synthetic/blue.png'],reportParams:{},abilityType:17,abilityParams:{first_frame:'synthetic/red.png',last_frame:'synthetic/blue.png',image_with_roles:[{role:'first_frame'}]}}};
 const result=await node.execute(context);
 assert.equal(result.content.abilityParams.input_box_content.user_input_content,'@HERO walks toward @END');
 assert(!JSON.stringify(result.content.abilityParams).includes('first_frame'));
 const evidence={scope:'Offline execution of captured frontend functions; dependencies mocked; no backend request',modules:{imageAttachmentMapper:120597,imageObjectBuilder:587594,videoSubmitNode:339259},input:{attachments,attachmentStates},output:{attachmentBlock:block,abilityParams:result.content.abilityParams,text:result.content.text},conclusions:['Image IDs and filenames are retained in separate attachment objects','Injected role and alias on image state are not serialized by this image mapper','Video abilityParams are rebuilt; injected first/last-frame fields at that upstream location are lost','Prompt aliases remain literal text; this node performs no alias-to-image-ID binding']};
 fs.writeFileSync(path.join(root,'offline_payload_probe.json'),JSON.stringify(evidence,null,2));
 console.log(JSON.stringify(evidence.output,null,2));console.log('PASS: captured image mapping and video parameter functions executed offline');
})().catch(e=>{console.error(e);process.exitCode=1});
