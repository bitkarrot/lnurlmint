<template id="page-lnurlmint">
  <div class="row q-col-gutter-md">
    <div class="col-12">
      <q-card>
        <q-card-section>
          <div class="row items-center q-mb-md">
            <div class="col">
              <h5 class="text-subtitle1 q-my-none">lnurlmint</h5>
            </div>
          </div>
          <q-linear-progress v-if="loading" color="primary" class="q-mb-md" />
          <q-list v-if="mints.length > 0" bordered separator>
            <q-item v-for="mint in mints" :key="mint.id">
              <q-item-section>
                <q-item-label>{{ mint.username }}</q-item-label>
                <q-item-label caption>{{ mint.id }}</q-item-label>
              </q-item-section>
            </q-item>
          </q-list>
          <div v-else-if="!loading" class="text-center text-grey q-pa-md">
            No mints yet.
          </div>
        </q-card-section>
      </q-card>
    </div>
  </div>
</template>
