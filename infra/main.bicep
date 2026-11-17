// main.bicep
// everything the cloud pipeline needs, in one resource group: storage, monitoring, a flex consumption function app,
// azure sql (free offer, auto-pauses instead of billing) and data factory with its pipelines and trigger.
// deployed twice by infra/deploy.py: first without functionKey, then with it, once the function code exists.

targetScope = 'resourceGroup'

@description('Azure region for everything (canadacentral by default).')
param location string = resourceGroup().location

@description('Short lowercase name used to build resource names.')
param prefix string = 'aqp'

@description('SQL admin login. Only used to apply the schema and create etl_app.')
param sqlAdminLogin string = 'aqadmin'

@secure()
param sqlAdminPassword string

@secure()
@description('Password for the least-privilege etl_app user that the functions and adf connect as.')
param sqlEtlPassword string

@secure()
param openaqApiKey string

@description('Public IP of the machine running deploy.py, so it can apply the sql files.')
param deployerIp string

@secure()
@description('Default function key. Empty on the first pass; deploy.py passes it on the second.')
param functionKey string = ''

@allowed(['daily', 'weekly'])
param refreshSchedule string = 'daily'

var suffix = uniqueString(resourceGroup().id)
var storageName = take('${prefix}st${suffix}', 24)
var functionName = '${prefix}-func-${suffix}'
var sqlServerName = '${prefix}-sql-${suffix}'
var factoryName = '${prefix}-adf-${suffix}'
var databaseName = 'airqualitydb'
var wireFunction = !empty(functionKey)

// --- storage: landing csvs, published json, model files, manual naps drops, and the function package ----

resource storage 'Microsoft.Storage/storageAccounts@2023-05-01' = {
  name: storageName
  location: location
  sku: { name: 'Standard_LRS' }
  kind: 'StorageV2'
  properties: {
    minimumTlsVersion: 'TLS1_2'
    allowBlobPublicAccess: false
    supportsHttpsTrafficOnly: true
  }
}

resource blobService 'Microsoft.Storage/storageAccounts/blobServices@2023-05-01' = {
  parent: storage
  name: 'default'
}

resource containers 'Microsoft.Storage/storageAccounts/blobServices/containers@2023-05-01' = [for name in [
  'landing', 'publish', 'models', 'manual', 'app-package'
]: {
  parent: blobService
  name: name
}]

// landing csvs are only needed until they're merged; keep two weeks for debugging, then let them go
resource lifecycle 'Microsoft.Storage/storageAccounts/managementPolicies@2023-05-01' = {
  parent: storage
  name: 'default'
  properties: {
    policy: {
      rules: [
        {
          name: 'expire-landing'
          enabled: true
          type: 'Lifecycle'
          definition: {
            filters: { blobTypes: ['blockBlob'], prefixMatch: ['landing/'] }
            actions: { baseBlob: { delete: { daysAfterModificationGreaterThan: 14 } } }
          }
        }
      ]
    }
  }
}

var storageConnection = 'DefaultEndpointsProtocol=https;AccountName=${storage.name};AccountKey=${storage.listKeys().keys[0].value};EndpointSuffix=${environment().suffixes.storage}'

// --- monitoring: small and capped ----------------------------------------------------------------------

resource logs 'Microsoft.OperationalInsights/workspaces@2023-09-01' = {
  name: '${prefix}-logs-${suffix}'
  location: location
  properties: {
    sku: { name: 'PerGB2018' }
    retentionInDays: 30
    workspaceCapping: { dailyQuotaGb: json('0.1') }
  }
}

resource insights 'Microsoft.Insights/components@2020-02-02' = {
  name: '${prefix}-ai-${suffix}'
  location: location
  kind: 'web'
  properties: {
    Application_Type: 'web'
    WorkspaceResourceId: logs.id
  }
}

// --- function app (flex consumption, python 3.13, 2 GB instances) -----------------------------------------

resource plan 'Microsoft.Web/serverfarms@2024-04-01' = {
  name: '${prefix}-plan-${suffix}'
  location: location
  kind: 'functionapp'
  sku: { name: 'FC1', tier: 'FlexConsumption' }
  properties: { reserved: true }
}

resource sqlServer 'Microsoft.Sql/servers@2023-08-01' = {
  name: sqlServerName
  location: location
  properties: {
    administratorLogin: sqlAdminLogin
    administratorLoginPassword: sqlAdminPassword
    minimalTlsVersion: '1.2'
    publicNetworkAccess: 'Enabled'
  }
}

var sqlHost = sqlServer.properties.fullyQualifiedDomainName

resource functionApp 'Microsoft.Web/sites@2024-04-01' = {
  name: functionName
  location: location
  kind: 'functionapp,linux'
  identity: { type: 'SystemAssigned' }
  properties: {
    serverFarmId: plan.id
    httpsOnly: true
    functionAppConfig: {
      deployment: {
        storage: {
          type: 'blobContainer'
          value: '${storage.properties.primaryEndpoints.blob}app-package'
          authentication: { type: 'SystemAssignedIdentity' }
        }
      }
      scaleAndConcurrency: {
        maximumInstanceCount: 40
        instanceMemoryMB: 2048
      }
      runtime: { name: 'python', version: '3.13' }
    }
    siteConfig: {
      minTlsVersion: '1.2'
      appSettings: [
        { name: 'AzureWebJobsStorage__accountName', value: storage.name }
        { name: 'APPLICATIONINSIGHTS_CONNECTION_STRING', value: insights.properties.ConnectionString }
        { name: 'STORAGE_MODE', value: 'blob' }
        { name: 'AZURE_STORAGE_CONNECTION_STRING', value: storageConnection }
        { name: 'SQL_CONN_STR', value: 'Driver={ODBC Driver 18 for SQL Server};Server=tcp:${sqlHost},1433;Database=${databaseName};Uid=etl_app;Pwd={AZ_SQL_ETL_PASSWORD};Encrypt=yes;TrustServerCertificate=no;Connection Timeout=120' }
        { name: 'AZ_SQL_ETL_PASSWORD', value: sqlEtlPassword }
        { name: 'OPENAQ_API_KEY', value: openaqApiKey }
        { name: 'OPENAQ_RPM', value: '50' }
        { name: 'LOCAL_DATA_DIR', value: '/tmp/data' }
        { name: 'DEV_CACHE', value: '0' }
        { name: 'REFRESH_SCHEDULE', value: refreshSchedule }
      ]
    }
  }
  dependsOn: [containers]
}

// the function reads and writes blobs (and its own package) with its managed identity
var blobDataOwner = 'b7e6dc6d-f1e8-4753-8033-0f276bb0955b'
resource functionBlobAccess 'Microsoft.Authorization/roleAssignments@2022-04-01' = {
  scope: storage
  name: guid(storage.id, functionApp.id, blobDataOwner)
  properties: {
    roleDefinitionId: subscriptionResourceId('Microsoft.Authorization/roleDefinitions', blobDataOwner)
    principalId: functionApp.identity.principalId
    principalType: 'ServicePrincipal'
  }
}

// --- azure sql: free offer, pauses when the free monthly amount runs out instead of billing --------------

resource allowAzure 'Microsoft.Sql/servers/firewallRules@2023-08-01' = {
  parent: sqlServer
  name: 'AllowAzureServices'
  properties: { startIpAddress: '0.0.0.0', endIpAddress: '0.0.0.0' }
}

resource allowDeployer 'Microsoft.Sql/servers/firewallRules@2023-08-01' = {
  parent: sqlServer
  name: 'AllowDeployer'
  properties: { startIpAddress: deployerIp, endIpAddress: deployerIp }
}

resource database 'Microsoft.Sql/servers/databases@2023-08-01' = {
  parent: sqlServer
  name: databaseName
  location: location
  sku: { name: 'GP_S_Gen5', tier: 'GeneralPurpose', family: 'Gen5', capacity: 2 }
  properties: {
    createMode: 'Default'
    maxSizeBytes: 34359738368
    requestedBackupStorageRedundancy: 'Local'
    useFreeLimit: true
    freeLimitExhaustionBehavior: 'AutoPause'
  }
}

// --- data factory ----------------------------------------------------------------------------------------

resource factory 'Microsoft.DataFactory/factories@2018-06-01' = {
  name: factoryName
  location: location
  identity: { type: 'SystemAssigned' }
  properties: {}
}

resource lsSql 'Microsoft.DataFactory/factories/linkedservices@2018-06-01' = {
  parent: factory
  name: 'ls_sql'
  properties: {
    type: 'AzureSqlDatabase'
    typeProperties: {
      // the free database may be paused; a long timeout lets the first connection wake it
      connectionString: 'Server=tcp:${sqlHost},1433;Database=${databaseName};User ID=etl_app;Password=${sqlEtlPassword};Encrypt=True;Connection Timeout=120'
    }
  }
}

resource lsBlob 'Microsoft.DataFactory/factories/linkedservices@2018-06-01' = {
  parent: factory
  name: 'ls_blob'
  properties: {
    type: 'AzureBlobStorage'
    typeProperties: { connectionString: storageConnection }
  }
}

resource lsFunction 'Microsoft.DataFactory/factories/linkedservices@2018-06-01' = if (wireFunction) {
  parent: factory
  name: 'ls_function'
  properties: {
    type: 'AzureFunction'
    typeProperties: {
      functionAppUrl: 'https://${functionApp.properties.defaultHostName}'
      functionKey: { type: 'SecureString', value: functionKey }
      authentication: 'Anonymous'
    }
  }
}

var dsLanding = loadJsonContent('../adf/dataset/ds_landing_csv.json')
var dsStg = loadJsonContent('../adf/dataset/ds_sql_stg.json')
var dsLookup = loadJsonContent('../adf/dataset/ds_sql_lookup.json')

resource datasetLanding 'Microsoft.DataFactory/factories/datasets@2018-06-01' = {
  parent: factory
  name: dsLanding.name
  properties: dsLanding.properties
  dependsOn: [lsBlob]
}

resource datasetStg 'Microsoft.DataFactory/factories/datasets@2018-06-01' = {
  parent: factory
  name: dsStg.name
  properties: dsStg.properties
  dependsOn: [lsSql]
}

resource datasetLookup 'Microsoft.DataFactory/factories/datasets@2018-06-01' = {
  parent: factory
  name: dsLookup.name
  properties: dsLookup.properties
  dependsOn: [lsSql]
}

var plLoadItem = loadJsonContent('../adf/pipeline/pl_load_item.json')
var plDaily = loadJsonContent('../adf/pipeline/pl_daily_refresh.json')

resource pipelineLoadItem 'Microsoft.DataFactory/factories/pipelines@2018-06-01' = if (wireFunction) {
  parent: factory
  name: plLoadItem.name
  properties: plLoadItem.properties
  dependsOn: [lsFunction, datasetLanding, datasetStg]
}

resource pipelineDaily 'Microsoft.DataFactory/factories/pipelines@2018-06-01' = if (wireFunction) {
  parent: factory
  name: plDaily.name
  properties: plDaily.properties
  dependsOn: [pipelineLoadItem, datasetLookup]
}

// 06:00 toronto time; weekly runs on mondays. deploy.py starts the trigger after the first deploy.
resource trigger 'Microsoft.DataFactory/factories/triggers@2018-06-01' = if (wireFunction) {
  parent: factory
  name: 'tr_refresh'
  properties: {
    type: 'ScheduleTrigger'
    typeProperties: {
      recurrence: {
        frequency: refreshSchedule == 'daily' ? 'Day' : 'Week'
        interval: 1
        startTime: '2026-11-18T06:00:00'
        timeZone: 'Eastern Standard Time'
        schedule: refreshSchedule == 'daily' ? { hours: [6], minutes: [0] } : { hours: [6], minutes: [0], weekDays: ['Monday'] }
      }
    }
    pipelines: [
      {
        pipelineReference: { referenceName: 'pl_daily_refresh', type: 'PipelineReference' }
        parameters: { mode: 'incremental' }
      }
    ]
  }
  dependsOn: [pipelineDaily]
}

output storageAccount string = storage.name
output functionAppName string = functionApp.name
output functionHost string = functionApp.properties.defaultHostName
output sqlServerName string = sqlServer.name
output sqlServerHost string = sqlHost
output databaseName string = database.name
output factoryName string = factory.name
