using System;
using System.Net.Http;
using System.Text;
using AMSHelper.Config;
using AMSHelper.Diagnostics;

namespace AMSHelper.m0BamboSpool
{
   public sealed class m0BamboSpoolClient
   {
      private readonly HttpClient _httpClient = new HttpClient();

      public bool AssignUid(int trayIndex, string uid)
      {
         if (string.IsNullOrEmpty(uid))
         {
            return false;
         }

         return this.SetTray(trayIndex, uid);
      }

      public bool ClearTray(int trayIndex)
      {
         return this.SetTray(trayIndex, "CLEAR");
      }

      private bool SetTray(int trayIndex, string uid)
      {
         if (string.IsNullOrEmpty(Configuration.m0BamboSpool.BaseUrl))
         {
            TraceWriter.WriteLine("[OSM] Host nicht konfiguriert.");
            return false;
         }

         string url = Configuration.m0BamboSpool.BaseUrl + "/ams/nfc/" + trayIndex.ToString() + "/set";
         string json = "{\"uid\":\"" + m0BamboSpoolClient.EscapeJson(uid) + "\"}";

         try
         {
            using (StringContent content = new StringContent(json, Encoding.UTF8, "application/json"))
            using (HttpResponseMessage response = _httpClient.Post(url, content))
            {
               int statusCode = (int)response.StatusCode;
               bool success = statusCode >= 200 && statusCode < 300;
               TraceWriter.WriteLine("[OSM] Tray " + trayIndex.ToString() + " -> " + uid + " | HTTP " + statusCode.ToString());
               return success;
            }
         }
         catch (Exception ex)
         {
            TraceWriter.WriteLine("[OSM] Tray " + trayIndex.ToString() + " Fehler: " + ex.Message);
            return false;
         }
      }

      private static string EscapeJson(string value)
      {
         if (value == null)
         {
            return string.Empty;
         }

         string escaped = string.Empty;
         for (int i = 0; i < value.Length; i++)
         {
            char character = value[i];
            if (character == '\\' || character == '"')
            {
               escaped += "\\";
            }
            escaped += character;
         }
         return escaped;
      }
   }
}
