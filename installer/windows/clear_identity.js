try {
  var fso = new ActiveXObject("Scripting.FileSystemObject");
  var path = Session.Property("AppDataFolder") + "TimeTracker\\identity.json";
  if (fso.FileExists(path)) fso.DeleteFile(path, true);
} catch(e) {}
