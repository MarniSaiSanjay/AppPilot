@description('Microsoft Entra object ID that needs read-only access to AppPilot telemetry.')
param readerPrincipalId string

@description('Storage account that contains AppPilot telemetry.')
param storageAccountName string = 'stapppilot'

@description('AppPilot telemetry table.')
param tableName string = 'SuiteRuns'

resource storageAccount 'Microsoft.Storage/storageAccounts@2023-05-01' existing = {
  name: storageAccountName
}

resource tableService 'Microsoft.Storage/storageAccounts/tableServices@2023-05-01' existing = {
  parent: storageAccount
  name: 'default'
}

resource telemetryTable 'Microsoft.Storage/storageAccounts/tableServices/tables@2023-05-01' existing = {
  parent: tableService
  name: tableName
}

var storageTableDataReaderRoleId = subscriptionResourceId(
  'Microsoft.Authorization/roleDefinitions',
  '76199698-9eea-4c19-bc75-cec21354c6b6'
)

resource telemetryReaderRole 'Microsoft.Authorization/roleAssignments@2022-04-01' = {
  name: guid(
    telemetryTable.id,
    readerPrincipalId,
    storageTableDataReaderRoleId
  )
  scope: telemetryTable
  properties: {
    principalId: readerPrincipalId
    principalType: 'User'
    roleDefinitionId: storageTableDataReaderRoleId
  }
}

output assignedScope string = telemetryTable.id
