using System.Threading;
using AMSHelper.Diagnostics;
using AMSHelper.Hardware;
using AMSHelper.Mqtt;
using AMSHelper.Network;
using AMSHelper.m0BamboSpool;

namespace AMSHelper.Ams
{
   public sealed class AmsHelperDevice : EspDevice
   {
      private BambuMqtt _mqtt;
      private string _amsHumidity = string.Empty;
      private string _amsHumidityRaw = string.Empty;
      private string _amsTemperature = string.Empty;

      public AmsHelperDevice() : base(Config.Configuration.Device.AmsSlotCount)
      {
      }

      public string AmsHumidity { get { return _amsHumidity; } }
      public string AmsHumidityRaw { get { return _amsHumidityRaw; } }
      public string AmsTemperature { get { return _amsTemperature; } }

      public void Start()
      {
         TraceWriter.WriteLine("================================");
         TraceWriter.WriteLine(Config.Configuration.Device.Name + " gestartet");
         TraceWriter.WriteLine("================================");
         var wifi = new WifiConnection();
         wifi.Connect();
         _mqtt = new BambuMqtt();
         var m0BamboSpool = new m0BamboSpoolClient();
         this.SetTray(0, new AmsTray(0, _mqtt, m0BamboSpool));
         this.SetTray(1, new AmsTray(1, _mqtt, m0BamboSpool));
         this.SetTray(2, new AmsTray(2, _mqtt, m0BamboSpool));
         this.SetTray(3, new AmsTray(3, _mqtt, m0BamboSpool));
         _mqtt.StatusUpdateReceived += this.BambuStatusUpdateReceived;
         for (int i = 0; i < this.Trays.Length; i++)
         {
            AmsTray tray = this.GetTray(i);
            if (tray != null)
            {
               tray.Start();
            }
         }
         Thread.Sleep(Config.Configuration.Bambu.InitialConnectDelayMs);
         _mqtt.Start();
         while (true)
         {
            Thread.Sleep(Config.Configuration.Device.MainLoopDelayMs);
         }
      }

      private void BambuStatusUpdateReceived(BambuStatusUpdate update)
      {
         if (update == null)
         {
            return;
         }
         if (update.HasAmsHumidity)
         {
            _amsHumidity = update.AmsHumidity;
         }
         if (update.HasAmsHumidityRaw)
         {
            _amsHumidityRaw = update.AmsHumidityRaw;
         }
         if (update.HasAmsTemperature)
         {
            _amsTemperature = update.AmsTemperature;
         }
         if (update.AmsOutputProduced)
         {
            this.WriteTrayStatus();
         }
      }

      private void WriteTrayStatus()
      {
         TraceWriter.WriteLine("[AMS] -------- Gesamtstatus --------");
         for (int i = 0; i < this.Trays.Length; i++)
         {
            AmsTray tray = this.GetTray(i);
            if (tray == null)
            {
               continue;
            }
            string line = "[AMS] Tray " + tray.Index + ": " + tray.Activity;
            if (tray.Activity != "BELEGT" && tray.Activity != "LEER")
            {
               line += tray.IsOccupied ? " | BELEGT" : " | LEER";
            }
            line += tray.Pn532Enabled ? " | PN532=aktiv" : " | PN532=deaktiviert";
            if (tray.Pn532Enabled)
            {
               line += tray.Uid.Length > 0 ? " | UID=" + tray.Uid : " | UID=noch nicht gelesen";
            }
            TraceWriter.WriteLine(line);
         }
         TraceWriter.WriteLine("[AMS] ------------------------------");
      }
   }
}
