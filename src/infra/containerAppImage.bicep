@description('Name of the chat Container App.')
param name string

@description('Whether the app already exists. Only a new app uses the bootstrap image.')
param exists bool

resource app 'Microsoft.App/containerApps@2024-03-01' existing = if (exists) {
  name: name
}

var bootstrapImage = 'mcr.microsoft.com/k8se/quickstart:latest'
var image = exists ? app!.properties.template.containers[0].image : bootstrapImage

output image string = image
output targetPort int = image == bootstrapImage ? 80 : 8000
