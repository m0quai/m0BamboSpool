namespace AMSHelper.Config
{
    public static class Configuration
    {
        public static class Device
        {
            public const string Name = "AMSHelper";
            public const int AmsSlotCount = 4;
            public const int MainLoopDelayMs = 200;
            public const int TrayHeartbeatIntervalMs = 5000;
        }

        public static class Wifi
        {
            public const string Ssid = "LRWN";
            public const string Password = "Network@Home4711007";
            public const bool RequiresDateTime = false;
            public const int ReconnectDelayMs = 5000;
        }

        public static class Bambu
        {
            public const string PrinterName = "Bubu";
            public const string PrinterIp = "192.168.52.84";
            public const string PrinterSerial = "01P00C612200050";
            public const string LanAccessCode = "60e81882";
            public const string MqttUsername = "bblp";
            public const int MqttPort = 8883;
            public const int MqttKeepAliveSeconds = 60;
            public const int MqttReconnectDelayMs = 5000;
            public const bool MqttUseTls = true;
            public const bool ValidateServerCertificate = false;
            public const int InitialConnectDelayMs = 4000;
            public static string MqttReportTopic => "device/" + PrinterSerial + "/report";
            public static string MqttRequestTopic => "device/" + PrinterSerial + "/request";
            public static string MqttClientId => "AMSHelper-" + PrinterSerial;
        }

        public static class m0BamboSpool
        {
            public const string Host = "192.168.52.73";
            public const int Port = 8000;
            public const int SpoolmanPort = 7912;
            public const int RequestTimeoutMs = 10000;
            public static string BaseUrl => Host.Length == 0 ? "" : "http://" + Host + ":" + Port;
        }

        public static class Nfc
        {
            public const bool Enabled = true;
            public const int StartupDelayMs = 1000;
            public const int ReadTimeoutMs = 1000;
            public const int PollingCycleTimeoutMs = 20000;
            public const int ReaderCount = 4;
            public const bool Tray0Enabled = true;
            public const bool Tray1Enabled = false;
            public const bool Tray2Enabled = false;
            public const bool Tray3Enabled = false;
            public const int I2cBus = 1;
            public const int Tray0I2cSdaPin = 8;
            public const int Tray0I2cSclPin = 9;
            public const int Tray1I2cSdaPin = -1;
            public const int Tray1I2cSclPin = -1;
            public const int Tray2I2cSdaPin = -1;
            public const int Tray2I2cSclPin = -1;
            public const int Tray3I2cSdaPin = -1;
            public const int Tray3I2cSclPin = -1;
            public const int Pn532I2cAddress = 0x24;
            public const int IdleDelayMs = 20;
            public const int ScanDelayMs = 50;
            public const int SpiBus = 1;
            public const int SpiClockPin = -1;
            public const int SpiMosiPin = -1;
            public const int SpiMisoPin = -1;
            public const int Reader1ChipSelectPin = -1;
            public const int Reader2ChipSelectPin = -1;
            public const int Reader3ChipSelectPin = -1;
            public const int Reader4ChipSelectPin = -1;
        }

        public static class Debugging
        {
            public const bool DumpAllBambuReports = true;
            public const bool DumpRawAmsStatusFields = false;
            public const bool DumpTelemetryOnlyBambuReports = false;
            public const bool DumpNtagPages = true;
            public const int TraceQueueSize = 128;
            public const int TraceWriterIdleDelayMs = 20;
            public const bool TraceHeartbeatEnabled = false;
        }
    }
}
