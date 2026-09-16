@description('Relay image reference.')
param image string

@description('ACS sender address.')
param senderAddress string

@description('Default recipient.')
param defaultRecipient string = ''

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

resource relayKeyVault 'Microsoft.KeyVault/vaults@2023-07-01' existing = {
  name: 'kvapppilotplg9b1'
}

resource communicationService 'Microsoft.Communication/communicationServices@2023-04-01' existing = {
  name: 'acs-apppilot'
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

resource relayKeyVaultRole 'Microsoft.Authorization/roleAssignments@2022-04-01' = {
  name: guid(
    relayKeyVault.id,
    relayIdentity.id,
    '4633458b-17de-408a-b874-0445c86b69e6'
  )
  scope: relayKeyVault
  properties: {
    principalId: relayIdentity.properties.principalId
    principalType: 'ServicePrincipal'
    roleDefinitionId: subscriptionResourceId(
      'Microsoft.Authorization/roleDefinitions',
      '4633458b-17de-408a-b874-0445c86b69e6'
    )
  }
}

resource relayRegistryPullRole 'Microsoft.Authorization/roleAssignments@2022-04-01' = {
  name: '76fe6fb8-8f1f-4046-9b8b-6dc3bd4b2422'
  scope: registry
  properties: {
    principalId: relayIdentity.properties.principalId
    principalType: 'ServicePrincipal'
    roleDefinitionId: subscriptionResourceId(
      'Microsoft.Authorization/roleDefinitions',
      '7f951dda-4ed3-4680-a7ca-43fe172d538d'
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
          value: communicationService.listKeys().primaryConnectionString
        }
        {
          name: 'api-key'
          keyVaultUrl: '${relayKeyVault.properties.vaultUri}secrets/apppilot-email-relay-key'
          identity: relayIdentity.id
        }
        {
          name: 'telemetry-api-key'
          keyVaultUrl: '${relayKeyVault.properties.vaultUri}secrets/apppilot-telemetry-relay-key'
          identity: relayIdentity.id
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
              secretRef: 'telemetry-api-key' // # allow-secret
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
    relayKeyVaultRole
    relayRegistryPullRole
  ]
}

output fqdn string = relay.properties.configuration.ingress.fqdn
