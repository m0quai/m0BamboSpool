using System;
using AMSHelper.Diagnostics;
using AMSHelper.Mqtt;
using AMSHelper.Hardware;
using AMSHelper.m0BamboSpool;

namespace AMSHelper.Ams
{
   public sealed class AmsTray
   {
      private enum TrayOperation
      {
         None,
         Loading,
         Unloading
      }

      private readonly BambuMqtt _mqtt;
      private readonly Pn532Device _pn532Device;
      private readonly m0BamboSpoolClient _m0BamboSpool;
      private string _uid = string.Empty;
      private string _activity = "Unbekannt";
      private string _lastSummary = string.Empty;
      private bool _occupied;
      private bool _occupiedKnown;
      private TrayOperation _operation = TrayOperation.None;
      private bool _isActiveTray;
      private bool _wasPreviousTray;
      private bool _isTargetTray;
      private bool _nfcUidCapturedInCycle;

      public AmsTray(int index, BambuMqtt mqtt, m0BamboSpoolClient m0BamboSpool)
      {
         this.Index = index;
         _mqtt = mqtt;
         _m0BamboSpool = m0BamboSpool;
         _pn532Device = new Pn532Device(index);
         _pn532Device.UidRead += this.Pn532UidRead;

         if (_mqtt != null)
         {
            _mqtt.StatusUpdateReceived += this.BambuStatusUpdateReceived;
         }
      }

      public int Index { get; private set; }
      public bool Pn532Enabled { get { return _pn532Device.Enabled; } }
      public string Uid { get { return _uid; } }
      public string Activity { get { return _activity; } }
      public bool IsOccupied { get { return _occupied; } }
      public bool IsNfcPolling { get { return _pn532Device.IsPolling; } }

      public void Start()
      {
         _pn532Device.Start();
      }

      private void BambuStatusUpdateReceived(BambuStatusUpdate update)
      {
         if (update == null)
         {
            return;
         }

         bool relevant = false;
         if (update.HasTrayExistBits)
         {
            int bits = BambuStatusParser.ParseTrayBits(update.TrayExistBits);
            if (this.SetOccupied((bits & (1 << this.Index)) != 0))
            {
               relevant = true;
            }
         }

         if (update.HasCommand && update.Command == "ams_get_rfid" && update.HasCommandSlotId)
         {
            int rfidSlot = BambuStatusParser.ParseTrayId(update.CommandSlotId);
            if (rfidSlot == this.Index)
            {
               _nfcUidCapturedInCycle = false;
               _uid = string.Empty;
               _lastSummary = string.Empty;
               if (this.StartNfcPolling())
               {
                  AmsTray.Write("[AMS] Tray " + this.Index + " -> NFC-Trigger durch ams_get_rfid");
                  relevant = true;
               }
            }
         }

         if (update.HasCommand && update.Command == "ams_change_filament")
         {
            int requested = this.GetRequestedTray(update);
            if (requested == this.Index)
            {
               if (this.BeginLoading())
               {
                  relevant = true;
               }
            }
            else if (requested == 255 && (_isActiveTray || _wasPreviousTray))
            {
               _operation = TrayOperation.Unloading;
               this.StopNfcPolling();
               if (this.SetActivity("ENTLADEN"))
               {
                  relevant = true;
               }
            }
         }

         if (update.HasTargetTray)
         {
            int target = BambuStatusParser.ParseTrayId(update.TargetTray);
            _isTargetTray = target == this.Index;
            if (_isTargetTray && !_isActiveTray)
            {
               if (this.BeginLoading())
               {
                  relevant = true;
               }
            }
            else if (target == 255 && (_isActiveTray || _wasPreviousTray || _operation != TrayOperation.None))
            {
               _operation = TrayOperation.Unloading;
               this.StopNfcPolling();
               if (this.SetActivity("ENTLADEN"))
               {
                  relevant = true;
               }
            }
         }

         if (update.HasPreviousTray)
         {
            int previous = BambuStatusParser.ParseTrayId(update.PreviousTray);
            _wasPreviousTray = previous == this.Index;
            if (_wasPreviousTray)
            {
               relevant = true;
            }
         }

         if (update.HasActiveTray)
         {
            int active = BambuStatusParser.ParseTrayId(update.ActiveTray);
            bool wasActive = _isActiveTray;
            _isActiveTray = active == this.Index;
            if (_isActiveTray)
            {
               _operation = TrayOperation.None;
               _isTargetTray = false;
               if (this.SetActivity("IN USE"))
               {
                  relevant = true;
               }
            }
            else if (active == 255 && wasActive && _operation == TrayOperation.Unloading)
            {
               if (this.SetActivity("ENTLADEN abgeschlossen / kein Filament aktiv"))
               {
                  relevant = true;
               }
            }
         }

         if (update.HasTrayReadingBits)
         {
            int bits = BambuStatusParser.ParseTrayBits(update.TrayReadingBits);
            bool reading = (bits & (1 << this.Index)) != 0;
            if (reading)
            {
               if (!_nfcUidCapturedInCycle)
               {
                  if (this.SetActivity("RFID wird gelesen"))
                  {
                     relevant = true;
                  }
                  if (this.StartNfcPolling())
                  {
                     relevant = true;
                  }
               }
            }
            else
            {
               bool pollingStopped = this.StopNfcPolling();
               if (_activity == "RFID wird gelesen")
               {
                  string nextActivity = _isActiveTray ? "IN USE" : (_operation == TrayOperation.Loading ? "LADEN" : "BEREIT");
                  if (this.SetActivity(nextActivity))
                  {
                     relevant = true;
                  }
               }
               if (pollingStopped)
               {
                  relevant = true;
               }
            }
         }

         if (update.HasTrayReadDoneBits)
         {
            int bits = BambuStatusParser.ParseTrayBits(update.TrayReadDoneBits);
            if ((bits & (1 << this.Index)) != 0)
            {
               this.StopNfcPolling();
               if (_activity == "RFID wird gelesen")
               {
                  this.SetActivity(_isActiveTray ? "IN USE" : (_operation == TrayOperation.Loading ? "LADEN" : "BEREIT"));
               }
               relevant = true;
            }
         }

         if (update.HasAmsStatus && _operation != TrayOperation.None)
         {
            bool completed;
            string activity = BambuStatusParser.InterpretAmsActivity(update.AmsStatus, _operation == TrayOperation.Unloading, out completed);
            if (!string.IsNullOrEmpty(activity))
            {
               if (this.SetActivity(activity))
               {
                  relevant = true;
               }
            }
            if (completed)
            {
               TrayOperation completedOperation = _operation;
               _operation = TrayOperation.None;
               _isTargetTray = false;
               this.StopNfcPolling();
               if (completedOperation == TrayOperation.Loading && _isActiveTray)
               {
                  if (this.SetActivity("IN USE"))
                  {
                     relevant = true;
                  }
               }
            }
            else if (_operation != TrayOperation.Unloading && BambuStatusParser.IsFilamentChangeStatus(update.AmsStatus))
            {
               _operation = TrayOperation.Loading;
            }
         }

         if (relevant)
         {
            update.AmsOutputProduced = true;
         }
      }

      private bool BeginLoading()
      {
         if (_isActiveTray)
         {
            _operation = TrayOperation.None;
            _isTargetTray = false;
            return this.SetActivity("IN USE");
         }

         bool changed = false;
         _operation = TrayOperation.Loading;
         _isTargetTray = true;
         if (this.SetActivity("LADEN"))
         {
            changed = true;
         }
         if (!_nfcUidCapturedInCycle && this.StartNfcPolling())
         {
            changed = true;
         }
         return changed;
      }

      private int GetRequestedTray(BambuStatusUpdate update)
      {
         int requested = -1;
         if (update.HasCommandSlotId)
         {
            requested = BambuStatusParser.ParseTrayId(update.CommandSlotId);
         }
         if (requested < 0 && update.HasCommandTarget)
         {
            requested = BambuStatusParser.ParseTrayId(update.CommandTarget);
         }
         return requested;
      }

      private bool SetOccupied(bool occupied)
      {
         bool changed = !_occupiedKnown || _occupied != occupied;
         _occupiedKnown = true;
         _occupied = occupied;
         if (!occupied)
         {
            if (changed && _m0BamboSpool != null)
            {
               _m0BamboSpool.ClearTray(this.Index);
            }
            _uid = string.Empty;
            _lastSummary = string.Empty;
            _isActiveTray = false;
            _wasPreviousTray = false;
            _isTargetTray = false;
            _operation = TrayOperation.None;
            _nfcUidCapturedInCycle = false;
            this.StopNfcPolling();
            return this.SetActivity("LEER");
         }
         if (_activity == "LEER" || _activity == "Unbekannt")
         {
            return this.SetActivity("BELEGT");
         }
         if (changed)
         {
            this.WriteSummaryIfStable();
         }
         return changed;
      }

      private bool SetActivity(string activity)
      {
         if (activity == null || _activity == activity)
         {
            return false;
         }
         _activity = activity;
         if (activity != "IN USE" && activity != "BELEGT" && activity != "BEREIT" && activity != "LEER" && activity != "LADEN" && activity != "ENTLADEN")
         {
            AmsTray.Write("[AMS] Tray " + this.Index + " -> " + activity);
         }
         else
         {
            this.WriteSummaryIfStable();
         }
         return true;
      }

      private bool StartNfcPolling()
      {
         if (_nfcUidCapturedInCycle)
         {
            return false;
         }
         bool started = _pn532Device.StartPolling();
         if (started && _pn532Device.Enabled)
         {
            AmsTray.Write("[AMS] Tray " + this.Index + " -> NFC-Polling START");
         }
         return started;
      }

      private bool StopNfcPolling()
      {
         bool stopped = _pn532Device.StopPolling();
         if (stopped && _pn532Device.Enabled)
         {
            AmsTray.Write("[AMS] Tray " + this.Index + " -> NFC-Polling ENDE");
         }
         if (stopped)
         {
            this.WriteSummaryIfStable();
         }
         return stopped;
      }

      private void Pn532UidRead(string uid)
      {
         if (string.IsNullOrEmpty(uid))
         {
            return;
         }
         _uid = uid;
         _nfcUidCapturedInCycle = true;
         _lastSummary = string.Empty;
         AmsTray.Write("[NFC] Tray " + this.Index + " UID=" + uid);
         if (_m0BamboSpool != null)
         {
            _m0BamboSpool.AssignUid(this.Index, uid);
         }
         this.StopNfcPolling();
         this.WriteSummaryIfStable();
      }

      private void WriteSummaryIfStable()
      {
         string activity = _activity;
         if (activity == "Unbekannt" && _occupiedKnown && _occupied)
         {
            activity = "BELEGT";
         }
         bool stable = activity == "BELEGT" || activity == "IN USE" || activity == "BEREIT" || activity == "LEER" || activity == "LADEN" || activity == "ENTLADEN";
         if (!stable)
         {
            return;
         }
         string info = "[AMS] Tray " + this.Index + ": " + activity;
         if (_pn532Device.Enabled)
         {
            info += " | PN532=aktiv";
            if (_uid.Length > 0)
            {
               info += " | UID=" + _uid;
            }
            else if (activity != "LEER")
            {
               info += " | UID=noch nicht gelesen";
            }
         }
         else
         {
            info += " | PN532=deaktiviert";
         }
         if (_lastSummary == info)
         {
            return;
         }
         _lastSummary = info;
         AmsTray.Write(info);
      }

      private static void Write(string text)
      {
         TraceWriter.WriteLine(text);
      }
   }
}
