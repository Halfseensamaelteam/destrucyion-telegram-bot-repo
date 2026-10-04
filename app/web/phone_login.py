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
</style>
</head>
<body>
<h1>Connect Telegram</h1>
<p id="message">This page is for a short-lived, one-time Telegram login attempt.</p>
<form id="login-form"><div id="fields"></div><button id="submit">Continue</button></form>
<script>
const token=new URLSearchParams(location.search).get("token");
const fields=document.getElementById("fields");
const message=document.getElementById("message");
const submit=document.getElementById("submit");
let stage="start";
function render(next){
 stage=next; fields.innerHTML="";
 if(next==="start") fields.innerHTML=
  '<input id="api_id" type="number" min="1" placeholder="API ID" required>'+
  '<input id="api_hash" placeholder="API Hash" required>'+
  '<input id="phone" placeholder="+628..." autocomplete="tel" required>';
 else if(next==="code") fields.innerHTML=
  '<input id="code" inputmode="numeric" autocomplete="one-time-code" placeholder="Telegram login code" required>';
 else if(next==="2fa") fields.innerHTML=
  '<input id="password" type="password" autocomplete="current-password" placeholder="Telegram 2FA password" required>';
 else {fields.innerHTML="<p>Connected successfully. You can close this page.</p>";submit.hidden=true;}
}
async function post(path,data){
 const r=await fetch(path+"?token="+encodeURIComponent(token),{
  method:"POST",headers:{"Content-Type":"application/json"},cache:"no-store",
  body:JSON.stringify(data)
 });
 const j=await r.json(); if(!r.ok) throw new Error(j.detail||"Request failed"); return j;
}
document.getElementById("login-form").addEventListener("submit",async e=>{
 e.preventDefault();
 try{
  let j;
  if(stage==="start") j=await post("/api/v1/phone-login/start",{
   api_id:Number(document.getElementById("api_id").value),
   api_hash:document.getElementById("api_hash").value.trim(),
   phone:document.getElementById("phone").value.trim()
  });
  else if(stage==="code") j=await post("/api/v1/phone-login/code",{
   code:document.getElementById("code").value.trim()
  });
  else if(stage==="2fa") j=await post("/api/v1/phone-login/2fa",{
   password:document.getElementById("password").value
  });
  message.textContent=j.message; render(j.stage);
 }catch(err){message.textContent=err.message;}
});
render("start");
</script>
</body>
</html>
"""
