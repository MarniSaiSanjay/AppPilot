@description('Relay image reference.')
param image string

@description('ACS sender address.')
param senderAddress string

@description('Default recipient.')
param defaultRecipient string = ''

@secure()
@description('Azure Communication Services connection string.')
param acsConnectionString string

@secure()
@description('Email API key.')
param emailApiKey string

@secure()
@description('Telemetry API key.')
param telemetryApiKey string

resource environment 'Microsoft.App/managedEnvironments@2024-03-01' existing = {
  name: 'cae-apppilot'
}

resource registry 'Microsoft.ContainerRegistry/registries@2023-07-01' existing = {
  name: 'acrapppilot'
}

resource relayIdentity 'Microsoft.ManagedIdentity/userAssignedIdentities@2023-01-31' existing = {
  name: 'id-apppilot-relay'
}

resource telemetryStorage 'Microsoft.Storage/storageAccounts@2023-05-01' existing = {
  name: 'stapppilot'
}

resource telemetryTable 'Microsoft.Storage/storageAccounts/tableServices/tables@2023-05-01' = {
  name: 'stapppilot/default/SuiteRuns'
}

resource telemetryTableRole 'Microsoft.Authorization/roleAssignments@2022-04-01' = {
  name: 'c165e8b1-a7e0-481e-9eec-37a22aae59c2'
  scope: telemetryTable
  properties: {
    principalId: relayIdentity.properties.principalId
    principalType: 'ServicePrincipal'
    roleDefinitionId: subscriptionResourceId(
      'Microsoft.Authorization/roleDefinitions',
      '0a9a7e1f-b9d0-4cc4-a60d-0319b160aaa3'
    )
  }
}

resource relay 'Microsoft.App/containerApps@2024-03-01' = {
  name: 'ca-apppilot-relay'
  location: resourceGroup().location
  identity: {
    type: 'UserAssigned'
    userAssignedIdentities: {
      '${relayIdentity.id}': {}
    }
  }
  properties: {
    environmentId: environment.id
    configuration: {
      activeRevisionsMode: 'Single'
      ingress: {
        external: true
        allowInsecure: false
        targetPort: 8000
        transport: 'auto'
        traffic: [
          {
            latestRevision: true
            weight: 100
          }
        ]
      }
      registries: [
        {
          server: registry.properties.loginServer
          identity: relayIdentity.id
        }
      ]
      secrets: [
        {
          name: 'acs-conn'
          value: acsConnectionString
        }
        {
          name: 'api-key'
          value: emailApiKey
        }
        {
          name: 'telemetry-api-key'
          value: telemetryApiKey
        }
      ]
    }
    template: {
      containers: [
        {
          name: 'relay'
          image: image
          resources: {
            cpu: json('0.5')
            memory: '1Gi'
          }
          env: [
            {
              name: 'ACS_CONNECTION_STRING'
              secretRef: 'acs-conn'
            }
            {
              name: 'ACS_SENDER_ADDRESS'
              value: senderAddress
            }
            {
              name: 'APPPILOT_EMAIL_TO'
              value: defaultRecipient
            }
            {
              name: 'APPPILOT_API_KEY'
              secretRef: 'api-key'
            }
            {
              name: 'APPPILOT_TELEMETRY_API_KEY'
              secretRef: 'telemetry-api-key'
            }
            {
              name: 'TELEMETRY_TABLE_ENDPOINT'
              value: telemetryStorage.properties.primaryEndpoints.table
            }
            {
              name: 'AZURE_CLIENT_ID'
              value: relayIdentity.properties.clientId
            }
          ]
          probes: [
            {
              type: 'Liveness'
              httpGet: {
                path: '/healthz'
                port: 8000
                scheme: 'HTTP'
              }
              initialDelaySeconds: 10
              periodSeconds: 30
              timeoutSeconds: 5
              failureThreshold: 3
            }
            {
              type: 'Readiness'
              httpGet: {
                path: '/healthz'
                port: 8000
                scheme: 'HTTP'
              }
              initialDelaySeconds: 5
              periodSeconds: 10
              timeoutSeconds: 5
              failureThreshold: 3
            }
          ]
        }
      ]
      scale: {
        minReplicas: 0
        maxReplicas: 2
        rules: [
          {
            name: 'http'
            http: {
              metadata: {
                concurrentRequests: '20'
              }
            }
          }
        ]
      }
    }
  }
  dependsOn: [
    telemetryTableRole
  ]
}

output fqdn string = relay.properties.configuration.ingress.fqdn
