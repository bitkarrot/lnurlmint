<template id="page-lnurlmint">
  <div class="row q-col-gutter-md">
    <div class="col-12">
      <q-card>
        <q-card-section>
          <div class="row items-center q-mb-md">
            <div class="col">
              <h5 class="text-subtitle1 q-my-none">lnurlmint</h5>
            </div>
            <div class="col-auto">
              <q-btn
                unelevated
                color="primary"
                label="Create Mint"
                @click="openCreateDialog"
              ></q-btn>
            </div>
          </div>

          <q-banner v-if="errorMessage" class="bg-negative text-white q-mb-md">
            {{ errorMessage }}
            <template v-slot:action>
              <q-btn flat label="Dismiss" @click="errorMessage = ''" />
            </template>
          </q-banner>

          <q-linear-progress v-if="loading" color="primary" class="q-mb-md" />

          <q-list v-if="mints.length > 0" bordered separator>
            <q-item v-for="mint in mints" :key="mint.id">
              <q-item-section>
                <q-item-label>{{ mint.username }}</q-item-label>
                <q-item-label caption>{{ mint.id }}</q-item-label>
              </q-item-section>
              <q-item-section side>
                <q-btn
                  flat
                  dense
                  color="negative"
                  icon="delete"
                  @click="deleteMint(mint.id)"
                ></q-btn>
              </q-item-section>
            </q-item>
          </q-list>
          <div v-else-if="!loading" class="text-center text-grey q-pa-md">
            No mints yet.
          </div>
        </q-card-section>
      </q-card>
    </div>

    <q-dialog v-model="createDialog.show">
      <q-card style="min-width: 350px">
        <q-card-section>
          <div class="text-h6">Create Mint</div>
        </q-card-section>
        <q-card-section>
          <q-form @submit.prevent="createMint">
            <q-input
              filled
              v-model="createDialog.data.username"
              label="Username"
              hint="A name for this mint"
              class="q-mb-md"
            ></q-input>
            <div class="row justify-end">
              <q-btn
                flat
                label="Cancel"
                v-close-popup
                class="q-mr-sm"
              ></q-btn>
              <q-btn
                unelevated
                color="primary"
                label="Create"
                type="submit"
                :loading="createDialog.loading"
              ></q-btn>
            </div>
          </q-form>
        </q-card-section>
      </q-card>
    </q-dialog>
  </div>
</template>
