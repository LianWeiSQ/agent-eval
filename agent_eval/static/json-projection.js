// Discard large JSON values while streaming; keep only the small result in memory.
class JsonProjection {
  constructor(skipKeys=[]) {
    this.skipKeys=new Set(skipKeys);this.parts=[];this.stack=[];
    this.inString=false;this.escaped=false;this.key=false;this.keyText='';
    this.skipNext=false;this.skipMode=null;this.skipDepth=0;this.skipString=false;this.skipEscaped=false;
    this.inputChars=0;this.outputChars=0;
  }
  append(text) {if(text){this.parts.push(text);this.outputChars+=text.length;}}
  feed(text) {
    this.inputChars+=text.length;
    let start=0;
    for(let i=0;i<text.length;i++) {
      const ch=text[i];
      if(this.skipMode) {
        if(this.skipMode==='primitive' && /[\s,\]}]/.test(ch)) {
          this.skipMode=null;start=i;
        } else {
          if(this.skipMode==='string' || this.skipString) {
            if(this.skipEscaped)this.skipEscaped=false;
            else if(ch==='\\')this.skipEscaped=true;
            else if(ch==='"'){if(this.skipMode==='string')this.skipMode=null;else this.skipString=false;}
          } else if(this.skipMode==='container') {
            if(ch==='"')this.skipString=true;
            else if(ch==='{' || ch==='[')this.skipDepth++;
            else if(ch==='}' || ch===']'){if(--this.skipDepth===0)this.skipMode=null;}
          }
          start=i+1;continue;
        }
      }
      if(this.skipNext && !/\s/.test(ch)) {
        this.append(text.slice(start,i));this.append('null');this.skipNext=false;
        this.skipEscaped=false;this.skipString=false;
        if(ch==='"')this.skipMode='string';
        else if(ch==='{' || ch==='['){this.skipMode='container';this.skipDepth=1;}
        else this.skipMode='primitive';
        start=i+1;continue;
      }
      if(this.inString) {
        if(this.key)this.keyText+=ch;
        if(this.escaped)this.escaped=false;
        else if(ch==='\\')this.escaped=true;
        else if(ch==='"') {
          this.inString=false;
          if(this.key) {
            const frame=this.stack[this.stack.length-1];
            frame.skipValue=this.skipKeys.has(JSON.parse(this.keyText));frame.expectKey=false;this.keyText='';
          }
        }
        continue;
      }
      const frame=this.stack[this.stack.length-1];
      if(ch==='"') {
        this.inString=true;this.key=!!(frame?.object && frame.expectKey);this.escaped=false;
        if(this.key)this.keyText='"';
      } else if(ch==='{')this.stack.push({object:true,expectKey:true});
      else if(ch==='[')this.stack.push({object:false});
      else if(ch==='}' || ch===']')this.stack.pop();
      else if(ch===',' && frame?.object)frame.expectKey=true;
      else if(ch===':' && frame?.skipValue){this.skipNext=true;frame.skipValue=false;}
    }
    if(!this.skipMode)this.append(text.slice(start));
  }
  finish() {
    if(this.inString || this.skipNext || (this.skipMode && this.skipMode!=='primitive') || this.stack.length)
      throw Error('服务返回的数据不完整，请重新加载。');
    const result=JSON.parse(this.parts.join(''));this.parts=[];return result;
  }
}
if(typeof module!=='undefined')module.exports={JsonProjection};
