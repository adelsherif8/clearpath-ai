FROM mcr.microsoft.com/dotnet/sdk:10.0 AS build
WORKDIR /src
COPY src/Reports.csproj src/
RUN dotnet restore src/Reports.csproj
COPY src/ src/
RUN dotnet publish src/Reports.csproj -c Release -o /app --no-restore

# Chiseled image: no shell, no package manager, runs as non-root (uid 1654).
FROM mcr.microsoft.com/dotnet/aspnet:10.0-noble-chiseled
WORKDIR /app
COPY --from=build /app .
ENV ASPNETCORE_HTTP_PORTS=8080 DOTNET_CLI_TELEMETRY_OPTOUT=1
EXPOSE 8080
ENTRYPOINT ["dotnet", "Reports.dll"]
