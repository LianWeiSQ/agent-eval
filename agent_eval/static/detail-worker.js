importScripts('/json-projection.js');
self.onmessage=async({data})=>{
  try {
    if(!/^\/api\/v1\/(jobs|trials)\/[a-zA-Z0-9_-]+(?:\/trace)?(?:\?[^#]*)?$/.test(data.path))throw Error('详情地址无效');
    const response=await fetch(data.path,{headers:{'X-Role':'project_admin'}});
    if(!(response.headers.get('content-type')||'').includes('json'))throw Error('服务返回了无法识别的响应');
    const projector=new JsonProjection(data.mode==='job'?['agent_run','grades','report']:[]);
    const reader=response.body.getReader(),decoder=new TextDecoder('utf-8');
    while(true){const {value,done}=await reader.read();if(done)break;projector.feed(decoder.decode(value,{stream:true}));}
    projector.feed(decoder.decode());
    const body=projector.finish();
    if(!response.ok || body.error)throw Error(body.error?.message||`HTTP ${response.status}`);
    self.postMessage({data:body.data,projection:{input_chars:projector.inputChars,output_chars:projector.outputChars}});
  }catch(error){self.postMessage({error:error.message||String(error)});}
};
