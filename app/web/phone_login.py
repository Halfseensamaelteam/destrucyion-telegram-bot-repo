"""Embedded browser UI for secure phone login."""
PHONE_LOGIN_HTML = r"""<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<meta name="referrer" content="no-referrer">
<meta http-equiv="Cache-Control" content="no-store">
<meta http-equiv="Pragma" content="no-cache">
<title>Telegram Login</title>
<style>
body{font-family:system-ui;max-width:520px;margin:40px auto;padding:16px}
input,button{width:100%;padding:12px;margin:6px 0;box-sizing:border-box}
button{cursor:pointer}
button:disabled,input:disabled{cursor:not-allowed;opacity:.6}
</style>
</head>
<body>
<h1>Connect Telegram</h1>
<p id="message">Checking this one-time login link...</p>
<form id="login-form"><div id="fields"></div><button id="submit" type="submit" disabled>Continue</button></form>
<script>
"use strict";
const token=new URLSearchParams(location.hash.slice(1)).get("token")||"";
const fields=document.getElementById("fields");
const message=document.getElementById("message");
const submit=document.getElementById("submit");
let stage="invalid";
let locked=false;

function lockPage(text){
 locked=true;
 stage="invalid";
 fields.replaceChildren();
 submit.disabled=true;
 message.textContent=text;
 if(location.hash) history.replaceState(null,"",location.pathname+location.search);
}

function addInput(id,type,placeholder,extra={}){
 const input=document.createElement("input");
 input.id=id;
 input.type=type;
 input.placeholder=placeholder;
 input.required=true;
 for(const [key,value] of Object.entries(extra)) input.setAttribute(key,value);
 fields.appendChild(input);
}

function render(next){
 stage=next;
 fields.replaceChildren();
 submit.hidden=false;
 submit.disabled=locked;
 if(next==="start"){
  addInput("api_id","number","API ID",{min:"1",inputmode:"numeric"});
  addInput("api_hash","text","API Hash");
  addInput("phone","tel","+628...",{autocomplete:"tel"});
 }else if(next==="code"){
  addInput("code","text","Telegram login code",{inputmode:"numeric",autocomplete:"one-time-code"});
 }else if(next==="2fa"){
  addInput("password","password","Telegram 2FA password",{autocomplete:"current-password"});
 }else if(next==="complete"){
  fields.replaceChildren();
  const p=document.createElement("p");
  p.textContent="Connected successfully. You can close this page.";
  fields.appendChild(p);
  submit.hidden=true;
 }
}

async function post(path,data){
 const r=await fetch(path,{
  method:"POST",
  headers:{"Content-Type":"application/json","X-Phone-Login-Token":token},
  cache:"no-store",
  body:JSON.stringify(data)
 });
 const contentType=r.headers.get("content-type")||"";
 let j=null;
 if(contentType.includes("application/json")){
  try{j=await r.json();}catch(_){j=null;}
 }
 if(!r.ok){
  throw new Error(j&&typeof j.detail==="string"?j.detail:"Request failed. Please start a new login link.");
 }
 if(!j||typeof j.stage!=="string"){
  throw new Error("Invalid server response.");
 }
 return j;
}

async function checkTicket(){
 if(!token){lockPage("This login link is missing its security token. Please request a new link.");return;}
 try{
  const r=await fetch("/api/v1/phone-login/status",{
   method:"GET",
   headers:{"X-Phone-Login-Token":token},
   cache:"no-store"
  });
  const contentType=r.headers.get("content-type")||"";
  const j=contentType.includes("application/json")?await r.json():null;
  if(!r.ok||!j||j.active!==true){
   lockPage("This login link is invalid, expired, or already used. Please request a new link.");
   return;
  }
  if(j.stage!=="phone"){
   lockPage("This login link has already been used. Please request a new link.");
   return;
  }
  message.textContent="Enter your Telegram API details and phone number.";
  locked=false;
  render("start");
 }catch(_){
  lockPage("Could not validate this login link. Please request a new link.");
 }
}

document.getElementById("login-form").addEventListener("submit",async e=>{
 e.preventDefault();
 if(locked)return;
 submit.disabled=true;
 try{
  let j;
  if(stage==="start")j=await post("/api/v1/phone-login/start",{
   api_id:Number(document.getElementById("api_id").value),
   api_hash:document.getElementById("api_hash").value.trim(),
   phone:document.getElementById("phone").value.trim()
  });
  else if(stage==="code")j=await post("/api/v1/phone-login/code",{
   code:document.getElementById("code").value.trim()
  });
  else if(stage==="2fa")j=await post("/api/v1/phone-login/2fa",{
   password:document.getElementById("password").value
  });
  else throw new Error("This login link is no longer active.");
  message.textContent=j.message;
  render(j.stage);
 }catch(err){
  message.textContent=err instanceof Error?err.message:"Request failed. Please try again.";
  if(/invalid|expired|already used|no longer active|request a new login/i.test(message.textContent))lockPage(message.textContent);
  else submit.disabled=false;
 }
});

checkTicket();
</script>
</body>
</html>
"""