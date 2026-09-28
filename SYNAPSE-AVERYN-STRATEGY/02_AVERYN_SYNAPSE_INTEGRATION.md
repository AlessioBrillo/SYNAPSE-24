# Averyn × Synapse Integration Specification
## Come Averyn Diventa la "Finestra" sui Dati Synapse — Senza Scrivere Firmware

---

## 🎯 Obiettivo dell'Integrazione

> **Averyn non tocca il firmware. Averyn consuma le interfacce standard che Synapse espone (BLE GATT, FIT files, MQTT, LSL) e le trasforma in valore per l'utente sportivo: Readiness, Sleep Quality, Stress Trends, HRV Analysis, Device Management.**

---

## 🔌 Interfacce Contrattuali (Synapse → Averyn)

| Interfaccia | Protocollo | Cosa Trasporta | Usato Da |
|-------------|------------|----------------|----------|
| **Live Metrics** | BLE GATT (Standard + Custom) | HR, HRV (RR intervals), Cadence, Speed, Temp, Battery, Signal Quality, Stress Triage | Averyn Mobile (tracking live, display real-time) |
| **Historical Sessions** | FIT File (Custom Developer Data) | Complete session: GPS, HR, HRV, Cadence, Temp, Stress, Signal Quality per record | Averyn Import (storico, analisi, trend) |
| **Cloud Sync** | MQTT/HTTPS → Synapse Cloud | Raw XDF, Batch inference results (Sleep, HRV Trends, Personalization) | Averyn Backend (SynapseCloudClient) |
| **Local Research** | LSL (Lab Streaming Layer) | Multi-stream sync: ECG, PPG, EEG, IMU, Markers | Ricercatori (non Averyn core) |
| **Device Mgmt** | BLE Custom Service | Provisioning (WiFi, Cloud URL, Cert), OTA, Config, Calibration | Averyn Mobile (Device Manager) |

---

## 📱 Averyn Mobile — Componenti Nuovi da Implementare

### 1. `BleSensorAdapter` → `SynapseBleAdapter`
```kotlin
// shared/tracking/src/commonMain/kotlin/averyn/tracking/adapter/SynapseBleAdapter.kt
class SynapseBleAdapter(
    private val context: Context,
    private val listener: SensorListener
) : BleSensorAdapter {

    // Services UUIDs (standard + custom)
    companion object {
        val SYNAPSE_SERVICE = UUID.fromString("53594E41-5053-452D-4241-4E44-000000000000")
        val CHAR_ECG_RAW = UUID.fromString("53594E41-5053-452D-4241-4E44-000000000001")
        val CHAR_PPG_RAW = UUID.fromString("53594E41-5053-452D-4241-4E44-000000000002")
        val CHAR_IMU_DATA = UUID.fromString("53594E41-5053-452D-4241-4E44-000000000003")
        val CHAR_SIGNAL_QUALITY = UUID.fromString("53594E41-5053-452D-4241-4E44-000000000004")
        val CHAR_STRESS_TRIAGE = UUID.fromString("53594E41-5053-452D-4241-4E44-000000000005")
        val CHAR_DEVICE_CONFIG = UUID.fromString("53594E41-5053-452D-4241-4E44-000000000006")
        val CHAR_PROVISIONING = UUID.fromString("53594E41-5053-452D-4241-4E44-000000000007")
    }

    // Standard GATT handling
    override fun onServicesDiscovered(gatt: BluetoothGatt, status: Int) {
        // 1. Enable HR notifications (0x2A37)
        // 2. Enable HRV/RR intervals (0x2A38 custom)
        // 3. Enable RSC/CSC per sport type
        // 4. Enable Battery (0x2A19)
        // 5. Enable Device Info (0x2A29)
        // 6. SUBSCRIBE Custom Synapse characteristics
        subscribeToSynapseCustomService(gatt)
    }

    private fun subscribeToSynapseCustomService(gatt: BluetoothGatt) {
        val service = gatt.getService(SYNAPSE_SERVICE)
        listOf(CHAR_ECG_RAW, CHAR_PPG_RAW, CHAR_IMU_DATA, 
               CHAR_SIGNAL_QUALITY, CHAR_STRESS_TRIAGE).forEach { uuid ->
            val char = service?.getCharacteristic(uuid)
            char?.let { gatt.setCharacteristicNotification(it, true) }
        }
    }

    override fun onCharacteristicChanged(gatt: BluetoothGatt, characteristic: BluetoothGattCharacteristic) {
        when (characteristic.uuid) {
            CHAR_ECG_RAW -> listener.onEcgRaw(parseEcgRaw(characteristic.value))
            CHAR_PPG_RAW -> listener.onPpgRaw(parsePpgRaw(characteristic.value))
            CHAR_IMU_DATA -> listener.onImuData(parseImuData(characteristic.value))
            CHAR_SIGNAL_QUALITY -> listener.onSignalQuality(parseSignalQuality(characteristic.value))
            CHAR_STRESS_TRIAGE -> listener.onStressTriage(parseStressTriage(characteristic.value))
            // Standard HR, RSC, CSC handled by parent BleSensorAdapter
        }
    }
}
```

**Output per Tracking Engine:**
- `LocationSample` arricchito con `heartRate`, `rrIntervals`, `cadence`, `skinTemperature`
- `SignalQualityReport` real-time (SQI, MAP, ECG quality) → UI feedback "Segnale scarso, regola fascia"
- `StressTriage` (baseline/stress/artifact) → mostra "Stress rilevato" durante attività

---

### 2. `SynapseFitImporter` — Import Storico da Band
```kotlin
// shared/tracking/src/commonMain/kotlin/averyn/tracking/import/SynapseFitImporter.kt
class SynapseFitImporter : FileImportAdapter {

    override fun supportedExtensions() = listOf("fit")

    override fun canImport(file: File): Boolean {
        return FitParser.parse(file).any { it.developerData?.any { it.name == "synapse_stress" } == true }
    }

    override fun importFile(file: File, userId: UserId): ImportResult {
        val fitFile = FitParser.parse(file)
        val activity = fitFile.toAverynActivity()
        
        // Estrae Developer Data Synapse
        fitFile.developerData?.forEach { devData ->
            when (devData.name) {
                "synapse_stress" -> activity.analysis.stress = parseStress(devData)
                "synapse_signal_quality" -> activity.analysis.signalQuality = parseSignalQuality(devData)
                "synapse_sleep_stage" -> activity.analysis.sleep = parseSleep(devData)
                "synapse_hrv_detail" -> activity.analysis.hrv = parseHrvDetail(devData)
            }
        }
        
        return ImportResult.Success(activity)
    }
}
```

**FIT Developer Data Fields (Synapse Custom):**
| Field Name | Type | Description |
|------------|------|-------------|
| `synapse_stress` | uint8 | 0=baseline, 1=stress, 2=artifact (per record) |
| `synapse_signal_quality` | struct | SQI (uint8), MAP (uint8), ECG_quality (uint8) |
| `synapse_sleep_stage` | uint8 | 0=wake, 1=light, 2=deep, 3=REM (per epoch 30s) |
| `synapse_hrv_detail` | struct | RMSSD, SDNN, pNN50, LF, HF, LF/HF (per finestra 5min) |

---

### 3. `SynapseDeviceManager` — Provisioning, OTA, Config
```kotlin
// apps/android/src/main/kotlin/averyn/device/SynapseDeviceManager.kt
class SynapseDeviceManager(
    private val bleAdapter: SynapseBleAdapter,
    private val cloudClient: SynapseCloudClient
) {

    // Provisioning Flow: "Aggiungi Dispositivo" in Averyn App
    suspend fun provisionNewBand(
        onProgress: (ProvisioningStep) -> Unit
    ): Result<SynapseDevice> = coroutineScope {
        onProgress(ProvisioningStep.SCANNING)
        val device = bleAdapter.scanForSynapseBand(timeout = 30s)
        
        onProgress(ProvisioningStep.CONNECTING)
        bleAdapter.connect(device)
        
        onProgress(ProvisioningStep.READING_INFO)
        val deviceInfo = bleAdapter.readDeviceInfo() // Serial, HW/FW rev, MAC
        
        onProgress(ProvisioningStep.WIFI_CREDENTIALS)
        val wifi = getWifiCredentialsFromUser() // UI: SSID + Password
        bleAdapter.writeProvisioningWifi(wifi.ssid, wifi.password)
        
        onProgress(ProvisioningStep.CLOUD_REGISTRATION)
        val cloudConfig = cloudClient.registerDevice(deviceInfo.serial) // Returns: cloud_url, device_cert, device_key
        bleAdapter.writeProvisioningCloud(cloudConfig.url, cloudConfig.cert, cloudConfig.key)
        
        onProgress(ProvisioningStep.COMMITTING)
        bleAdapter.writeProvisioningCommit()
        
        onProgress(ProvisioningStep.VERIFYING)
        val verified = bleAdapter.waitForProvisioningSuccess(timeout = 60s)
        
        if (verified) {
            val synapseDevice = SynapseDevice(
                id = deviceInfo.serial,
                name = "Synapse Band ${deviceInfo.serial.last(4)}",
                firmwareVersion = deviceInfo.fwRev,
                hardwareVersion = deviceInfo.hwRev,
                registeredAt = Clock.System.now(),
                cloudConnected = true
            )
            cloudClient.linkDeviceToUser(synapseDevice.id, currentUser.id)
            Result.Success(synapseDevice)
        } else {
            Result.Failure(ProvisioningException("Timeout o errore verifica"))
        }
    }

    // OTA Update
    suspend fun checkAndApplyOta(device: SynapseDevice): Result<Unit> {
        val latest = cloudClient.getLatestFirmware(device.hardwareVersion)
        if (latest.version > device.firmwareVersion) {
            val firmware = cloudClient.downloadFirmware(latest.url, latest.signature)
            bleAdapter.startOta(firmware) // Progress via BLE notify
            // Wait for reboot + reconnect + version verify
        }
    }

    // Device Settings Sync
    suspend fun syncSettings(device: SynapseDevice, settings: SynapseSettings) {
        bleAdapter.writeDeviceConfig(settings.toProtobuf())
        cloudClient.updateDeviceSettings(device.id, settings)
    }
}
```

---

### 4. Nuove Metriche Versionate (Shared Module)
```kotlin
// shared/metrics/src/commonMain/kotlin/averyn/metrics/ReadinessMetric.kt
@MetricDefinition(
    name = "Readiness",
    version = "readiness-synapse-v1",
    unit = "score (0-100)",
    description = "Composite readiness from HRV trend, sleep quality, stress load, recovery",
    algorithmVersion = "readiness-synapse-v1"
)
data class ReadinessMetric(
    val score: Int,                    // 0-100
    val hrvComponent: Int,             // 0-100 (HRV vs baseline personale)
    val sleepComponent: Int,           // 0-100 (sleep score notte precedente)
    val stressComponent: Int,          // 0-100 (stress load inverso)
    val recoveryComponent: Int,        // 0-100 (recovery from training load)
    val factors: List<ReadinessFactor> // Es. ["HRV below baseline", "Poor sleep"]
)

// shared/metrics/src/commonMain/kotlin/averyn/metrics/SleepQualityMetric.kt
@MetricDefinition(
    name = "Sleep Quality",
    version = "sleep-synapse-v1",
    unit = "score (0-100)",
    algorithmVersion = "sleep-synapse-v1"
)
data class SleepQualityMetric(
    val score: Int,
    val totalSleepTime: Duration,
    val deepSleepPercent: Float,
    val remPercent: Float,
    val awakePercent: Float,
    val sleepEfficiency: Float,
    val hrvDuringSleep: HrvSummary,
    val stagingAlgorithm: String = "YASA-on-Synapse-Cloud"
)

// shared/metrics/src/commonMain/kotlin/averyn/metrics/StressTrendMetric.kt
@MetricDefinition(
    name = "Daily Stress Trend",
    version = "stress-synapse-v1",
    unit = "stress_index (0-100)",
    algorithmVersion = "stress-synapse-v1"
)
data class StressTrendMetric(
    val dailyAverage: Float,
    val peakStress: Float,
    val recoveryPeriods: Int,
    val stressEpisodes: List<StressEpisode>,
    val classificationAlgorithm: String = "WESAD-3class-TFLM-edge"
)
```

---

### 5. `SynapseCloudClient` — Backend Ktor → Synapse Cloud
```kotlin
// backend/src/main/kotlin/averyn/backend/synapse/SynapseCloudClient.kt
class SynapseCloudClient(
    private val httpClient: HttpClient,
    private val config: SynapseCloudConfig,
    private val tokenProvider: ServiceAccountTokenProvider
) {

    private val baseUrl = config.baseUrl // es. https://synapse-cloud.miodominio.com

    // Chiama Synapse Cloud REST API con service account token
    private suspend fun <T> get(path: String, type: Type): Result<T> {
        val token = tokenProvider.getToken() // JWT RS256, audience=synapse-cloud
        return httpClient.get("$baseUrl$path").bearerAuth(token).body(type)
    }

    // Readiness score per utente (calcolato da cloud batch inference)
    suspend fun getReadiness(userId: UserId, date: LocalDate): Result<ReadinessMetric> =
        get("/api/v1/metrics/readiness?user_id=$userId&date=$date", ReadinessMetric::class.java)

    // Sleep staging results
    suspend fun getSleepQuality(userId: UserId, date: LocalDate): Result<SleepQualityMetric> =
        get("/api/v1/metrics/sleep?user_id=$userId&date=$date", SleepQualityMetric::class.java)

    // HRV Trends (7/30 giorni)
    suspend fun getHrvTrends(userId: UserId, days: Int): Result<HrvTrendMetric> =
        get("/api/v1/metrics/hrv/trends?user_id=$userId&days=$days", HrvTrendMetric::class.java)

    // Stress daily summary
    suspend fun getStressTrend(userId: UserId, date: LocalDate): Result<StressTrendMetric> =
        get("/api/v1/metrics/stress?user_id=$userId&date=$date", StressTrendMetric::class.java)

    // Device management
    suspend fun registerDevice(serial: String): Result<DeviceRegistration> =
        httpClient.post("$baseUrl/api/v1/devices/register").body(DeviceRegistrationRequest(serial))
    
    suspend fun linkDeviceToUser(deviceId: String, userId: UserId): Result<Unit> =
        httpClient.post("$baseUrl/api/v1/devices/$deviceId/link").body(LinkDeviceRequest(userId))

    suspend fun getLatestFirmware(hwVersion: String): Result<FirmwareInfo> =
        get("$baseUrl/api/v1/firmware/latest?hw_version=$hwVersion", FirmwareInfo::class.java)

    suspend fun downloadFirmware(url: String, expectedSignature: String): Result<ByteArray> {
        val bytes = httpClient.get(url).body<ByteArray>()
        return if (verifyEd25519Signature(bytes, expectedSignature, config.publicKey)) 
            Result.Success(bytes) 
        else Result.Failure(SecurityException("Firma firmware non valida"))
    }
}
```

---

## 🌐 Averyn Web — Sezione Synapse (React/TS)

### Struttura Pagine
```
/synapse                           # Dashboard dispositivi Synapse
/synapse/devices                   # Lista device, stato, battery, firmware
/synapse/devices/:id               # Detail: provisioning, settings, calibrazione, OTA
/synapse/readiness                 # Readiness score giornaliero + trend 7/30 giorni
/synapse/sleep                     # Sleep staging dettagliato (hypnogram, fasi, HRV notturno)
/synapse/stress                    # Stress timeline, episodi, recupero, correlazione attività
/synapse/hrv                       # HRV analysis: time domain, frequency domain, trend
/synapse/signal-quality            # Signal quality report per sessione
/synapse/export                    # Export dati: FIT, XDF, CSV per ricerca
```

### Componenti Chiave
```tsx
// apps/web/src/components/synapse/ReadinessCard.tsx
export const ReadinessCard: React.FC<{ metric: ReadinessMetric }> = ({ metric }) => (
  <Card className="readiness-card">
    <CardHeader>
      <Icon name="battery-charging" />
      <span>Readiness</span>
      <ScoreCircle score={metric.score} /> {/* 0-100, color coded */}
    </CardHeader>
    <CardContent>
      <FactorBars factors={metric.factors} />
      <TrendSparkline data={metric.history7d} />
    </CardContent>
    <CardActions>
      <Button onClick={() => navigate('/synapse/readiness')}>Dettaglio</Button>
    </CardActions>
  </Card>
)

// apps/web/src/components/synapse/SleepHypnogram.tsx
export const SleepHypnogram: React.FC<{ sleep: SleepQualityMetric }> = ({ sleep }) => (
  <Chart>
    <HypnogramData stages={sleep.hypnogramEpochs} /> {/* 30s epochs: wake/light/deep/rem */}
    <OverlayHRV hrvData={sleep.hrvDuringSleep} />
    <Annotations events={sleep.disturbances} />
  </Chart>
)

// apps/web/src/components/synapse/DeviceManager.tsx
export const DeviceManager: React.FC = () => {
  const devices = useSynapseDevices()
  return (
    <DeviceList>
      {devices.map(device => (
        <DeviceCard key={device.id} device={device}>
          <BatteryIndicator level={device.batteryLevel} charging={device.charging} />
          <FirmwareBadge version={device.firmwareVersion} latest={device.latestAvailable} />
          <Button onClick={() => startOta(device)} disabled={!device.otaAvailable}>
            Aggiorna Firmware
          </Button>
          <Button onClick={() => navigate(`/synapse/devices/${device.id}/calibrate`)}>
            Calibra Sensori
          </Button>
        </DeviceCard>
      ))}
    </DeviceList>
  )
}
```

---

## 🔐 Consent & Privacy Model

```kotlin
// shared/domain/src/commonMain/kotlin/averyn/domain/user/SynapseConsent.kt
data class SynapseConsent(
    val userId: UserId,
    val deviceId: String,
    val grantedAt: Instant,
    val revokedAt: Instant?,
    val scopes: Set<ConsentScope> = setOf(
        ConsentScope.HRV_ANALYSIS,           // HRV trends, readiness
        ConsentScope.SLEEP_STAGING,          // Sleep analysis (YASA)
        ConsentScope.STRESS_MONITORING,      // Stress triage, episodes
        ConsentScope.SIGNAL_QUALITY,         // Signal quality reports
        ConsentScope.RAW_DATA_EXPORT,        // Export XDF/FIT raw
        ConsentScope.PERSONALIZATION,        // Model personalization on cloud
        ConsentScope.RESEARCH_SHARING        // Opt-in: anonymized data for research
    )
)

// Backend: middleware che verifica consent prima di chiamare Synapse Cloud
fun SynapseConsentGuard(scope: ConsentScope): RouteInterceptor = { call ->
    val consent = consentRepository.getCurrent(call.userId, call.deviceId)
    if (scope in consent.scopes) call.proceed() else call.respond(403, "Consent required for $scope")
}
```

---

## 🧪 Test Plan Integrazione

| Test | Descrizione | Criterio Pass |
|------|-------------|---------------|
| **BLE Connect** | Connetti Band → Averyn App, ricevi HR/HRV/Cadence live | HR ±2 bpm vs reference, RR intervals ricevuti |
| **FIT Import** | Importa .FIT da Band → Averyn crea Activity con metriche Synapse | Sleep stages, stress, HRV detail presenti in analysis |
| **Provisioning** | Nuovo Band → Averyn App → WiFi + Cloud registrazione → Successo | Device appare in cloud, mTLS funziona |
| **OTA Update** | Firmware v1.0.0 → v1.1.0 via Averyn App | Band riavvia, versione aggiornata, dati preservati |
| **Cloud Sync** | Attività registrata → Sync → Synapse Cloud inference → Averyn mostra Readiness | Readiness score appare < 5 min post-sync |
| **Self-Host** | Docker Compose Averyn + Synapse Cloud locale → Tutto funziona | Nessuna dipendenza esterna, Tailscale OK |
| **Consent Revoke** | Utente revoca `SLEEP_STAGING` → Cloud non processa più sleep | Nuove notti non hanno sleep analysis |

---

## 📋 Definition of Done Integrazione (MVP)

- [ ] `SynapseBleAdapter` funziona su Android + iOS (background capable)
- [ ] `SynapseFitImporter` importa .FIT validi da Band (testati con 10+ sessioni reali)
- [ ] `SynapseDeviceManager` provisioning end-to-end < 3 min per utente non tecnico
- [ ] `SynapseCloudClient` chiama Synapse Cloud REST API con service account auth
- [ ] Metriche `Readiness`, `SleepQuality`, `StressTrend`, `HrvTrend` versionate in `shared/metrics`
- [ ] Web Synapse Section: Readiness, Sleep, Stress, HRV, Devices, Export
- [ ] Consent model implementato e testato (grant/revoke per scope)
- [ ] Documentazione utente: "Come collegare Synapse Band ad Averyn"

---

## 💡 Perché Questa Architettura Funziona

1. **Zero coupling firmware-app**: Averyn usa BLE standard + FIT standard. Se domani fai `SynapseBand v2` con stesso GATT, Averyn non cambia.
2. **Cloud separato = scalabilità**: Synapse Cloud fa ML pesante (YASA, personalizzazione). Averyn Backend rimane leggero (Ktor + PostGIS).
3. **Self-host reale**: Utente lancia `docker-compose up` → ha Averyn + Synapse Cloud locale. Zero lock-in.
4. **Open ecosystem**: Qualsiasi app può integrare Synapse Band (BLE GATT + FIT pubblici). Averyn è "best client" non "only client".
5. **Business aligned**: Più Band vendi → più utenti su Averyn → più valore cloud → margine hardware copre costi.

---

*Specifica viva. Aggiornata ad ogni sprint. Versione 1.0 — 27 Settembre 2026*